
# ==============================================================================
# PROCUREAI — services/itc_reconciliation.py
# ITC Reconciliation Service — the core of Feature 1.
#
# FRESHER EXPLANATION:
# This service answers one question every month for every customer:
# "How much of Ramesh's GST input tax credit is at risk?"
#
# It does this by comparing two data sources:
#
# SOURCE A — Our purchases table
# What Ramesh tells us he bought:
# - Sharma Metals, Invoice INV-2847, ₹10,152 GST
# - Kumar Tools, Invoice INV-1203, ₹25,000 GST
# Total eligible ITC: ₹35,152
#
# SOURCE B — GSTR-2B from GSTN
# What suppliers actually filed:
# - Sharma Metals, INV-2847 ✓ (filed correctly)
# - Kumar Tools, INV-1203 ✗ (NOT in GSTR-2B)
#
# RESULT:
# Matched ITC: ₹10,152 (Sharma Metals — safe)
# At risk ITC: ₹25,000 (Kumar Tools — not filed)
#
# ACTION:
# Alert Ramesh: "Kumar Tools has not filed GSTR-1 for INV-1203.
# ₹25,000 ITC at risk. Call them today."
# ==============================================================================

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from procureai.integrations.sandbox_gst import (
    gst_client,
    GSTR2BData,
    GSTNAPIError,
    GSTNDataNotAvailableError,
    InvalidGSTINError,
)
from procureai.models.customer import Customer
from procureai.models.itc import ITCTracking
from procureai.models.purchase import Purchase
from procureai.schemas.itc import (
    ITCReconciliationResult,
    ITCSupplierBreakdown,
    ITCAlertData,
    ITCFeeCalculation,
)
from procureai.schemas.purchase import MissingGSTR2BItem


# ==============================================================================
# HELPER — TAX PERIOD DISPLAY
# Converts "082026" to "August 2026"
# ==============================================================================

MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March",
    "04": "April", "05": "May", "06": "June",
    "07": "July", "08": "August", "09": "September",
    "10": "October", "11": "November", "12": "December",
}

def tax_period_to_display(tax_period: str) -> str:
    """
    Convert MMYYYY to human readable.
    "082026" → "August 2026"
    """
    month = tax_period[:2]
    year = tax_period[2:]
    return f"{MONTH_NAMES.get(month, month)} {year}"


def get_previous_period(tax_period: str) -> str:
    """
    Get the previous tax period.
    "082026" → "072026"
    "012026" → "122025"

    FRESHER NOTE:
    We need the previous period to calculate recovery.
    Recovery = ITC that was at_risk last month but appeared this month.
    """
    month = int(tax_period[:2])
    year = int(tax_period[2:])

    if month == 1:
        return f"12{year - 1}"
    return f"{month - 1:02d}{year}"


# ==============================================================================
# ITC RECONCILIATION SERVICE
# ==============================================================================

class ITCReconciliationService:
    """
    Orchestrates the monthly ITC reconciliation for all customers.

    FRESHER NOTE ON SERVICE CLASSES:
    A service class groups related business logic together.
    All ITC-related operations live here — fetching, comparing,
    calculating, saving, alerting.
    The router calls this service. The Celery task calls this service.
    The service doesn't know or care who called it.
    """

    async def reconcile_customer(
        self,
        customer_id: str,
        tax_period: str,
        db: AsyncSession,
        force_rerun: bool = False,
    ) -> ITCReconciliationResult:
        """
        Run ITC reconciliation for one customer for one tax period.

        This is the main method — everything else supports this.

        Args:
            customer_id: UUID of the customer
            tax_period: MMYYYY format e.g. "082026"
            db: Database session
            force_rerun: Re-run even if already done this period

        Returns:
            ITCReconciliationResult with full reconciliation details

        FRESHER NOTE ON THE FLOW:
        Step 1: Load customer from database
        Step 2: Check if already reconciled (skip if so)
        Step 3: Load purchases for this period
        Step 4: Fetch GSTR-2B from GSTN
        Step 5: Compare — find missing invoices
        Step 6: Calculate ITC amounts
        Step 7: Save results to database
        Step 8: Return result (caller will trigger alert)
        """
        logger.info(
            f"Starting ITC reconciliation: "
            f"customer={customer_id}, period={tax_period}"
        )

        # ── Step 1: Load customer ──────────────────────────────────────────
        customer = await self._get_customer(customer_id, db)
        if not customer:
            raise ValueError(f"Customer not found: {customer_id}")

        if not customer.is_active:
            logger.info(f"Skipping inactive customer: {customer_id}")
            raise ValueError(f"Customer {customer_id} is inactive")

        # ── Step 2: Check if already reconciled ───────────────────────────
        existing = await self._get_existing_record(
            customer_id, tax_period, db
        )

        if existing and not force_rerun:
            if existing.reconciliation_status == "completed":
                logger.info(
                    f"Already reconciled for {customer_id} "
                    f"period {tax_period} — skipping"
                )
                return self._result_from_record(existing)

        # ── Step 3: Mark as running ───────────────────────────────────────
        await self._upsert_itc_record(
            customer_id=customer_id,
            tax_period=tax_period,
            status="running",
            db=db,
        )

        try:
            # ── Step 4: Load purchases from database ──────────────────────
            purchases = await self._get_customer_purchases(
                customer_id, tax_period, db
            )

            if not purchases:
                logger.warning(
                    f"No purchases found for {customer_id} "
                    f"period {tax_period}"
                )
                return await self._save_empty_result(
                    customer, tax_period, db
                )

            logger.info(
                f"Found {len(purchases)} purchases for "
                f"{customer.gstin} period {tax_period}"
            )

            # ── Step 5: Fetch GSTR-2B from GSTN ──────────────────────────
            gstr2b = await self._fetch_gstr2b(customer, tax_period)

            # ── Step 6: Compare purchases vs GSTR-2B ─────────────────────
            supplier_breakdown = self._compare_purchases_to_gstr2b(
                purchases, gstr2b
            )

            # ── Step 7: Calculate ITC amounts ─────────────────────────────
            eligible_itc = sum(
                p.gst_amount for p in purchases if p.itc_eligible
            )
            matched_itc = sum(
                p.gst_amount
                for p in purchases
                if p.itc_eligible and p.in_gstr2b
            )
            at_risk_itc = eligible_itc - matched_itc
            missing_count = sum(
                1 for p in purchases
                if p.itc_eligible and not p.in_gstr2b
            )

            # ── Step 8: Calculate recovery vs previous period ─────────────
            recovered_itc = await self._calculate_recovery(
                customer_id, tax_period, db
            )

            # ── Step 9: Save results to database ──────────────────────────
            await self._upsert_itc_record(
                customer_id=customer_id,
                tax_period=tax_period,
                status="completed",
                eligible_itc=eligible_itc,
                matched_itc=matched_itc,
                at_risk_itc=at_risk_itc,
                recovered_itc=recovered_itc,
                missing_invoice_count=missing_count,
                gstr2b_raw=str(gstr2b.raw_response),
                db=db,
            )

            # ── Step 10: Update individual purchase records ───────────────
            await self._update_purchase_gstr2b_status(
                purchases, gstr2b, db
            )

            logger.info(
                f"Reconciliation complete for {customer.gstin}: "
                f"eligible=₹{eligible_itc}, "
                f"matched=₹{matched_itc}, "
                f"at_risk=₹{at_risk_itc}, "
                f"recovered=₹{recovered_itc}"
            )

            # Build and return result
            return ITCReconciliationResult(
                customer_id=customer_id,
                tax_period=tax_period,
                period_display=tax_period_to_display(tax_period),
                eligible_itc=eligible_itc,
                matched_itc=matched_itc,
                at_risk_itc=at_risk_itc,
                recovered_itc=recovered_itc,
                supplier_breakdown=supplier_breakdown,
                missing_invoice_count=missing_count,
                reconciled_at=datetime.now(timezone.utc),
            )

        except Exception as e:
            # Mark as failed in database
            await self._upsert_itc_record(
                customer_id=customer_id,
                tax_period=tax_period,
                status="failed",
                error_message=str(e),
                db=db,
            )
            logger.error(
                f"Reconciliation failed for {customer_id}: {e}"
            )
            raise

    async def reconcile_all_customers(
        self,
        tax_period: str,
        db: AsyncSession,
        cluster_filter: Optional[str] = None,
    ) -> dict:
        """
        Run reconciliation for ALL active customers.
        Called by Celery Beat on the 16th of every month.

        FRESHER NOTE ON BATCH PROCESSING:
        We process customers one at a time, not all at once.
        Why? Because each reconciliation makes external API calls
        (GSTN) that have rate limits. Running 40 simultaneously
        would hit rate limits and get all requests rejected.
        Sequential processing is slower but reliable.
        """
        logger.info(
            f"Starting batch ITC reconciliation for period {tax_period}"
        )

        # Get all active customers
        query = select(Customer).where(Customer.is_active == True)
        if cluster_filter:
            query = query.where(Customer.cluster == cluster_filter)

        result = await db.execute(query)
        customers = result.scalars().all()

        logger.info(f"Processing {len(customers)} customers")

        results = {
            "period": tax_period,
            "total": len(customers),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "failures": [],
        }

        for customer in customers:
            try:
                await self.reconcile_customer(
                    customer_id=str(customer.id),
                    tax_period=tax_period,
                    db=db,
                )
                results["success"] += 1

            except ValueError as e:
                # Expected errors (inactive, no purchases)
                results["skipped"] += 1
                logger.info(f"Skipped {customer.gstin}: {e}")

            except Exception as e:
                results["failed"] += 1
                results["failures"].append({
                    "customer_id": str(customer.id),
                    "gstin": customer.gstin,
                    "error": str(e),
                })
                logger.error(
                    f"Failed for {customer.gstin}: {e}"
                )
                # Continue to next customer — don't stop batch
                continue

        logger.info(
            f"Batch complete: {results['success']} success, "
            f"{results['failed']} failed, "
            f"{results['skipped']} skipped"
        )

        return results

    def build_alert_data(
        self,
        customer: Customer,
        result: ITCReconciliationResult,
    ) -> Optional[ITCAlertData]:
        """
        Build the alert data structure for the messaging service.

        Returns None if no alert is needed
        (everything filed correctly, no ITC at risk).

        FRESHER NOTE:
        We separate reconciliation from alerting.
        Reconcile first → store results → then decide to alert.
        This lets us:
        1. Re-alert without re-reconciling
        2. Skip alerts for customers who've opted out
        3. Test reconciliation without sending real messages
        """
        if not result.has_risk and result.recovered_itc == 0:
            logger.info(
                f"No alert needed for {customer.gstin} — "
                f"all ITC matched, no recovery to report"
            )
            return None

        return ITCAlertData(
            customer_id=str(customer.id),
            owner_name=customer.owner_name,
            business_name=customer.business_name,
            preferred_channel=customer.preferred_channel,
            preferred_language=customer.preferred_language,
            phone=customer.phone,
            tax_period=result.tax_period,
            period_display=result.period_display,
            total_at_risk=result.at_risk_itc,
            missing_supplier_count=len(result.suppliers_with_risk),
            missing_invoice_count=result.missing_invoice_count,
            suppliers_to_contact=result.suppliers_with_risk,
            previous_period_recovery=result.recovered_itc,
        )

    def calculate_fee(
        self,
        customer_id: str,
        tax_period: str,
        recovered_itc: Decimal,
    ) -> ITCFeeCalculation:
        """
        Calculate ProcureAI's fee for this period's recovery.

        Fee = recovered_itc × 20%
        CA share = fee × 20%
        Net revenue = fee - CA share

        Called after reconciliation when recovered_itc > 0.
        """
        period_display = tax_period_to_display(tax_period)

        return ITCFeeCalculation(
            customer_id=customer_id,
            tax_period=tax_period,
            period_display=period_display,
            recovered_itc=recovered_itc,
        )

    # ==========================================================================
    # PRIVATE HELPER METHODS
    # Internal methods used by reconcile_customer.
    # Named with _ prefix — convention for private methods in Python.
    # ==========================================================================

    async def _get_customer(
        self, customer_id: str, db: AsyncSession
    ) -> Optional[Customer]:
        """Load customer from database by ID."""
        result = await db.execute(
            select(Customer).where(
                Customer.id == uuid.UUID(customer_id)
            )
        )
        return result.scalar_one_or_none()

    async def _get_existing_record(
        self,
        customer_id: str,
        tax_period: str,
        db: AsyncSession,
    ) -> Optional[ITCTracking]:
        """Check if ITC record already exists for this period."""
        result = await db.execute(
            select(ITCTracking).where(
                and_(
                    ITCTracking.customer_id == uuid.UUID(customer_id),
                    ITCTracking.tax_period == tax_period,
                )
            )
        )
        return result.scalar_one_or_none()

    async def _get_customer_purchases(
        self,
        customer_id: str,
        tax_period: str,
        db: AsyncSession,
    ) -> list[Purchase]:
        """
        Load all eligible purchases for this customer and period.

        FRESHER NOTE:
        We filter to itc_eligible=True.
        Some purchases (personal expenses, motor vehicles) are NOT
        eligible for ITC. We skip those in reconciliation.
        """
        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.customer_id == uuid.UUID(customer_id),
                    Purchase.tax_period == tax_period,
                    Purchase.itc_eligible == True,
                )
            )
        )
        return result.scalars().all()

    async def _fetch_gstr2b(
        self,
        customer: Customer,
        tax_period: str,
    ) -> GSTR2BData:
        """
        Fetch GSTR-2B from GSTN via Sandbox.co.in.
        Returns mock data in development.
        """
        try:
            return await gst_client.fetch_gstr2b(
                gstin=customer.gstin,
                tax_period=tax_period,
            )
        except GSTNDataNotAvailableError:
            logger.warning(
                f"GSTR-2B not available yet for {customer.gstin} "
                f"period {tax_period}"
            )
            raise
        except InvalidGSTINError:
            logger.error(
                f"Invalid GSTIN for customer {customer.id}: "
                f"{customer.gstin}"
            )
            raise
        except GSTNAPIError as e:
            logger.error(
                f"GSTN API error for {customer.gstin}: {e}"
            )
            raise

    def _compare_purchases_to_gstr2b(
        self,
        purchases: list[Purchase],
        gstr2b: GSTR2BData,
    ) -> list[ITCSupplierBreakdown]:
        """
        Compare purchase invoices against GSTR-2B data.
        Returns breakdown per supplier.

        FRESHER NOTE ON THE ALGORITHM:
        For each purchase invoice:
        1. Check if (supplier_gstin, invoice_number) is in GSTR-2B
        2. If YES → mark as matched, add GST to matched_itc
        3. If NO → mark as at_risk, add to missing_invoices list

        Group results by supplier for the alert:
        "Sharma Metals: all filed ✓"
        "Kumar Tools: INV-1203 missing ✗ — ₹25,000 at risk"
        """
        # Group purchases by supplier
        supplier_purchases: dict[str, list[Purchase]] = {}
        for purchase in purchases:
            gstin = purchase.supplier_gstin
            if gstin not in supplier_purchases:
                supplier_purchases[gstin] = []
            supplier_purchases[gstin].append(purchase)

        breakdowns = []

        for supplier_gstin, sup_purchases in supplier_purchases.items():
            total_itc = Decimal("0")
            matched_itc = Decimal("0")
            missing_invoices = []

            for purchase in sup_purchases:
                total_itc += purchase.gst_amount

                is_matched = gstr2b.is_invoice_present(
                    supplier_gstin,
                    purchase.invoice_number,
                )

                if is_matched:
                    matched_itc += purchase.gst_amount
                else:
                    missing_invoices.append(
                        MissingGSTR2BItem(
                            supplier_name=purchase.supplier_name,
                            supplier_gstin=purchase.supplier_gstin,
                            supplier_phone=purchase.supplier_phone,
                            invoice_number=purchase.invoice_number,
                            invoice_date=purchase.invoice_date,
                            gst_amount=purchase.gst_amount,
                        )
                    )

            at_risk = total_itc - matched_itc

            breakdowns.append(
                ITCSupplierBreakdown(
                    supplier_gstin=supplier_gstin,
                    supplier_name=sup_purchases[0].supplier_name,
                    supplier_phone=sup_purchases[0].supplier_phone,
                    total_itc=total_itc,
                    matched_itc=matched_itc,
                    at_risk_itc=at_risk,
                    missing_invoices=missing_invoices,
                )
            )

        return breakdowns

    async def _calculate_recovery(
        self,
        customer_id: str,
        tax_period: str,
        db: AsyncSession,
    ) -> Decimal:
        """
        Calculate ITC recovered since last period.

        Recovery = ITC that was at_risk in previous period
                   but now appears in GSTR-2B.

        FRESHER NOTE:
        This is how we prove our value to Ramesh.
        "Last month ₹59,000 was at risk. This month it appeared.
        We recovered ₹59,000 for you. Our fee: ₹11,800."

        Algorithm:
        1. Get previous period's at_risk_itc
        2. Get current period's matched invoices that were
           previously unmatched
        3. Recovery = amount that moved from at_risk to matched
        """
        prev_period = get_previous_period(tax_period)

        prev_record = await self._get_existing_record(
            customer_id, prev_period, db
        )

        if not prev_record or prev_record.at_risk_itc == 0:
            return Decimal("0")

        # Count purchases that were at_risk last period
        # but now appear in GSTR-2B this period
        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.customer_id == uuid.UUID(customer_id),
                    Purchase.tax_period == prev_period,
                    Purchase.itc_eligible == True,
                    Purchase.in_gstr2b == True,
                    Purchase.gstr2b_matched_at.isnot(None),
                )
            )
        )
        newly_matched = result.scalars().all()

        # Recovery = sum of GST for invoices that just got matched
        recovery = sum(p.gst_amount for p in newly_matched)
        return Decimal(str(recovery))

    async def _update_purchase_gstr2b_status(
        self,
        purchases: list[Purchase],
        gstr2b: GSTR2BData,
        db: AsyncSession,
    ) -> None:
        """
        Update each purchase record with its GSTR-2B match status.

        For matched invoices: in_gstr2b=True, gstr2b_matched_at=today
        For unmatched invoices: in_gstr2b=False (unchanged)

        FRESHER NOTE:
        This is important for recovery calculation next month.
        When an invoice appears in GSTR-2B, we record the date.
        Next month, we check: "was this invoice previously at_risk?"
        If yes → count it as recovered.
        """
        today = datetime.now(timezone.utc).date().isoformat()

        for purchase in purchases:
            is_matched = gstr2b.is_invoice_present(
                purchase.supplier_gstin,
                purchase.invoice_number,
            )

            if is_matched and not purchase.in_gstr2b:
                # Newly matched this month
                purchase.in_gstr2b = True
                purchase.gstr2b_matched_at = today
                db.add(purchase)

        await db.flush()

    async def _upsert_itc_record(
        self,
        customer_id: str,
        tax_period: str,
        status: str,
        db: AsyncSession,
        eligible_itc: Decimal = Decimal("0"),
        matched_itc: Decimal = Decimal("0"),
        at_risk_itc: Decimal = Decimal("0"),
        recovered_itc: Decimal = Decimal("0"),
        missing_invoice_count: int = 0,
        gstr2b_raw: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Insert or update ITC tracking record.

        FRESHER NOTE ON UPSERT:
        "Upsert" = INSERT if not exists, UPDATE if exists.
        PostgreSQL supports this with ON CONFLICT DO UPDATE.
        We use this because reconciliation might run multiple times
        for the same period (retry after failure, force_rerun).
        We never want duplicate records — upsert guarantees one row
        per customer per period.
        """
        period_display = tax_period_to_display(tax_period)
        now = datetime.now(timezone.utc).isoformat()

        fee_amount = (
            recovered_itc * Decimal("0.20")
        ).quantize(Decimal("0.01"))

        ca_share = (
            fee_amount * Decimal("0.20")
        ).quantize(Decimal("0.01"))

        stmt = insert(ITCTracking).values(
            id=uuid.uuid4(),
            customer_id=uuid.UUID(customer_id),
            tax_period=tax_period,
            period_display=period_display,
            eligible_itc=eligible_itc,
            matched_itc=matched_itc,
            at_risk_itc=at_risk_itc,
            recovered_itc=recovered_itc,
            missing_invoice_count=missing_invoice_count,
            fee_rate=Decimal("0.20"),
            fee_amount=fee_amount,
            ca_share_amount=ca_share,
            reconciliation_status=status,
            last_reconciled_at=now,
            gstr2b_raw_response=gstr2b_raw,
            error_message=error_message,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ).on_conflict_do_update(
            index_elements=["customer_id", "tax_period"],
            set_={
                "eligible_itc": eligible_itc,
                "matched_itc": matched_itc,
                "at_risk_itc": at_risk_itc,
                "recovered_itc": recovered_itc,
                "missing_invoice_count": missing_invoice_count,
                "fee_amount": fee_amount,
                "ca_share_amount": ca_share,
                "reconciliation_status": status,
                "last_reconciled_at": now,
                "gstr2b_raw_response": gstr2b_raw,
                "error_message": error_message,
                "updated_at": datetime.now(timezone.utc),
            }
        )

        await db.execute(stmt)
        await db.flush()

    def _result_from_record(
        self, record: ITCTracking
    ) -> ITCReconciliationResult:
        """Convert existing database record to result schema."""
        return ITCReconciliationResult(
            customer_id=str(record.customer_id),
            tax_period=record.tax_period,
            period_display=record.period_display,
            eligible_itc=record.eligible_itc,
            matched_itc=record.matched_itc,
            at_risk_itc=record.at_risk_itc,
            recovered_itc=record.recovered_itc,
            supplier_breakdown=[],
            missing_invoice_count=record.missing_invoice_count,
            reconciled_at=datetime.now(timezone.utc),
        )

    async def _save_empty_result(
        self,
        customer: Customer,
        tax_period: str,
        db: AsyncSession,
    ) -> ITCReconciliationResult:
        """Save and return empty result when no purchases found."""
        await self._upsert_itc_record(
            customer_id=str(customer.id),
            tax_period=tax_period,
            status="completed",
            db=db,
        )
        return ITCReconciliationResult(
            customer_id=str(customer.id),
            tax_period=tax_period,
            period_display=tax_period_to_display(tax_period),
            eligible_itc=Decimal("0"),
            matched_itc=Decimal("0"),
            at_risk_itc=Decimal("0"),
            recovered_itc=Decimal("0"),
            supplier_breakdown=[],
            missing_invoice_count=0,
            reconciled_at=datetime.now(timezone.utc),
        )


# ==============================================================================
# MODULE-LEVEL SERVICE INSTANCE
# ==============================================================================

itc_service = ITCReconciliationService()