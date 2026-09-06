
# ==============================================================================
# PROCUREAI — services/gem_service.py
# GeM Tender scanning service.
# ==============================================================================

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.integrations.gem_scanner import gem_client, GemTender
from procureai.integrations.telegram_bot import telegram_client
from procureai.models.customer import Customer


class GemService:
    """
    Orchestrates GeM tender scanning for all customers.
    """

    async def scan_all_customers(
        self, db: AsyncSession
    ) -> dict:
        """
        Scan GeM for all active customers with product categories.
        Called daily by GitHub Actions at 7 AM IST.
        """
        logger.info("Starting GeM scan for all customers")

        # Get customers with product categories set
        result = await db.execute(
            select(Customer).where(
                and_(
                    Customer.is_active == True,
                    Customer.telegram_chat_id.isnot(None),
                )
            )
        )
        customers = result.scalars().all()

        results = {
            "total_customers": len(customers),
            "tenders_found": 0,
            "alerts_sent": 0,
            "failed": 0,
        }

        for customer in customers:
            try:
                customer_profile = {
                    "owner_name": customer.owner_name,
                    "business_name": customer.business_name,
                    "product_categories": customer.product_categories,
                    "cluster": customer.cluster,
                    "udyam_number": customer.udyam_number,
                    "turnover_band": customer.turnover_band,
                }

                eligible_tenders = await gem_client.scan_for_customer(
                    customer_profile
                )

                if eligible_tenders:
                    results["tenders_found"] += len(eligible_tenders)

                    # Send alert for each eligible tender
                    for tender, eligibility in eligible_tenders[:3]:
                        await self._send_tender_alert(
                            customer=customer,
                            tender=tender,
                            eligibility=eligibility,
                        )
                        results["alerts_sent"] += 1

            except Exception as e:
                logger.error(
                    f"GeM scan failed for {customer.gstin}: {e}"
                )
                results["failed"] += 1
                continue

        logger.info(
            f"GeM scan complete: "
            f"{results['tenders_found']} tenders found, "
            f"{results['alerts_sent']} alerts sent"
        )

        return results

    async def _send_tender_alert(
        self,
        customer: Customer,
        tender: GemTender,
        eligibility,
    ) -> None:
        """
        Send GeM tender alert to customer via Telegram.
        """
        if not customer.telegram_chat_id:
            return

        urgency_emoji = "🚨" if tender.is_urgent else "🏛️"
        confidence_emoji = {
            "high": "✅",
            "medium": "⚠️",
            "low": "❓"
        }.get(eligibility.confidence, "⚠️")

        lines = [
            f"{urgency_emoji} <b>ProcureAI — GeM Tender Alert</b>",
            "",
            f"<b>{tender.title}</b>",
            "",
            f"🏢 {tender.department}",
            f"💰 Estimated value: <b>{tender.formatted_value}</b>",
            f"📦 Quantity: {tender.quantity} {tender.unit}",
            f"📍 Location: {tender.location}",
            f"📅 Deadline: {tender.end_date}",
            f"⏰ {tender.days_remaining} days remaining",
            "",
            f"{confidence_emoji} Eligibility: {eligibility.confidence.upper()}",
        ]

        if tender.mse_exemption:
            lines.append("🏷️ MSE Exemption available")

        if tender.is_urgent:
            lines.append("")
            lines.append("⚠️ <b>URGENT — Deadline within 3 days!</b>")

        lines.extend([
            "",
            f"🔢 Bid: {tender.bid_number}",
            "",
            "— ProcureAI",
        ])

        message = "\n".join(lines)

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔗 View Tender",
                        "url": tender.bid_url,
                    }
                ],
                [
                    {
                        "text": "✅ Interested",
                        "callback_data": f"gem_interested_{tender.bid_number}"
                    },
                    {
                        "text": "❌ Not relevant",
                        "callback_data": f"gem_dismiss_{tender.bid_number}"
                    }
                ]
            ]
        }

        await telegram_client.send_message(
            chat_id=customer.telegram_chat_id,
            text=message,
            reply_markup=reply_markup,
        )

        logger.info(
            f"GeM alert sent to {customer.owner_name}: "
            f"{tender.bid_number} {tender.formatted_value}"
        )


gem_service = GemService()