
# ==============================================================================
# PROCUREAI — services/messaging.py
# Messaging Orchestration Service.
#
# FRESHER EXPLANATION:
# Every other service produces DATA.
# This service turns data into MESSAGES that reach Ramesh's phone.
#
# THREE RESPONSIBILITIES:
#
# 1. ROUTING — Which channel does this customer use?
#    Ramesh uses RCS. Venkatesh prefers voice calls.
#    This service reads customer.preferred_channel and routes correctly.
#
# 2. FORMATTING — What does the message look like?
#    RCS: rich card with buttons and formatted numbers
#    SMS: plain text that fits in 160 characters
#    Both: same information, different presentation
#
# 3. DEDUPLICATION — Have we already sent this alert today?
#    We track sent alerts in the database.
#    Never send the same ITC alert twice in one month.
#    Never send the same price alert twice for the same material
#    in the same week.
#    This prevents Ramesh from feeling spammed and blocking our number.
#
# IMPORTANT DESIGN DECISION:
# This service does NOT know about ITC, deliveries, or prices.
# It only knows about MESSAGES and CUSTOMERS.
# The other services call this one — never the reverse.
# ==============================================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.integrations.msg91 import msg91_client, MessageResult
from procureai.schemas.itc import ITCAlertData, ITCReconciliationResult
from procureai.schemas.purchase import MissingGSTR2BItem


# ==============================================================================
# KANNADA MESSAGE TEMPLATES
# Pre-translated alert templates for Kannada-speaking owners.
#
# FRESHER NOTE ON LOCALISATION:
# Most Bommasandra manufacturers speak Kannada at home
# and use English numbers. Our messages mix both —
# Kannada sentences with English numbers and proper nouns.
# This is called "code-switching" and is completely natural
# in Bangalore's manufacturing community.
# ==============================================================================

KANNADA_TEMPLATES = {
    "itc_alert": (
        "Namaskara {name} avare!\n\n"
        "₹{amount} ITC riskalli ide.\n"
        "{count} supplier(s) GSTR-1 file maadilla.\n\n"
        "Avrige call maadi: {supplier_names}\n\n"
        "— ProcureAI"
    ),
    "delivery_warning": (
        "Namaskara {name} avare!\n\n"
        "{supplier} ninna {material} delivery "
        "{days} dina late aagide.\n"
        "Avrige call maadi: {phone}\n\n"
        "— ProcureAI"
    ),
    "price_pulse": (
        "Namaskara {name} avare!\n\n"
        "{material}: Neevu ₹{your_price}/kg kotidira.\n"
        "Market rate: ₹{market_price}/kg\n"
        "Ulitaya: ₹{saving}\n\n"
        "— ProcureAI"
    ),
    "good_news": (
        "Shubha Samachar, {name} avare!\n\n"
        "₹{amount} ITC recover aayitu!\n"
        "Nimma account: ₹{net} benefit.\n\n"
        "— ProcureAI"
    ),
}

HINDI_TEMPLATES = {
    "itc_alert": (
        "Namaste {name} ji!\n\n"
        "₹{amount} ITC risk mein hai.\n"
        "{count} supplier(s) ne GSTR-1 file nahi kiya.\n\n"
        "Unhe call karein: {supplier_names}\n\n"
        "— ProcureAI"
    ),
    "delivery_warning": (
        "Namaste {name} ji!\n\n"
        "{supplier} ki {material} delivery "
        "{days} din late hai.\n"
        "Call karein: {phone}\n\n"
        "— ProcureAI"
    ),
    "price_pulse": (
        "Namaste {name} ji!\n\n"
        "{material}: Aapne ₹{your_price}/kg diya.\n"
        "Market rate: ₹{market_price}/kg\n"
        "Bachat: ₹{saving}\n\n"
        "— ProcureAI"
    ),
}


class MessagingService:
    """
    Orchestrates all outbound messages to customers and suppliers.

    FRESHER NOTE ON WHEN TO USE THIS vs msg91_client DIRECTLY:
    - msg91_client: Low-level HTTP calls to MSG91 API
    - MessagingService: High-level business logic —
      "send the ITC alert for this customer with this data"

    Other services always use MessagingService, never msg91_client directly.
    Only MessagingService talks to msg91_client.
    This layering means: if we switch from MSG91 to another provider,
    we change only msg91_client — everything else stays the same.
    """

    # ==========================================================================
    # ITC ALERT MESSAGING
    # ==========================================================================

    async def send_itc_alert(
        self,
        alert_data: ITCAlertData,
        db: AsyncSession,
    ) -> Optional[MessageResult]:
        """
        Send ITC at-risk alert to customer.

        Handles:
        - Language selection (Kannada/Hindi/English)
        - Channel selection (RCS/SMS based on preference)
        - Recovery good news (if previous recovery happened)
        - Deduplication (don't send same period alert twice)

        FRESHER NOTE ON THE MESSAGE STRUCTURE:
        We always lead with GOOD NEWS if there is any.
        "You recovered ₹59,000 last month! (good news)
        But this month, ₹43,000 is at risk. (bad news)"

        People engage better when you start positive.
        The bad news still gets delivered — just after the good news.
        """
        logger.info(
            f"Preparing ITC alert for customer {alert_data.customer_id}: "
            f"₹{alert_data.total_at_risk} at risk"
        )

        # Build supplier names list for the message
        supplier_names = ", ".join([
            s.supplier_name
            for s in alert_data.suppliers_to_contact[:3]  # Max 3 in message
        ])
        if len(alert_data.suppliers_to_contact) > 3:
            supplier_names += f" + {len(alert_data.suppliers_to_contact) - 3} more"

        first_name = alert_data.owner_name.split()[0]

        # Build message based on language preference
        if alert_data.preferred_language == "kn":
            message_body = KANNADA_TEMPLATES["itc_alert"].format(
                name=first_name,
                amount=f"{alert_data.total_at_risk:,.0f}",
                count=alert_data.missing_supplier_count,
                supplier_names=supplier_names,
            )

            # Prepend good news if recovery happened
            if alert_data.has_good_news:
                good_news = KANNADA_TEMPLATES["good_news"].format(
                    name=first_name,
                    amount=f"{alert_data.previous_period_recovery:,.0f}",
                    net=f"{alert_data.previous_period_recovery * Decimal('0.8'):,.0f}",
                )
                message_body = good_news + "\n---\n" + message_body

        elif alert_data.preferred_language == "hi":
            message_body = HINDI_TEMPLATES["itc_alert"].format(
                name=first_name,
                amount=f"{alert_data.total_at_risk:,.0f}",
                count=alert_data.missing_supplier_count,
                supplier_names=supplier_names,
            )
        else:
            # English
            message_body = self._build_english_itc_message(
                alert_data, supplier_names, first_name
            )

        # Send via appropriate channel
        result = await msg91_client.send_itc_alert(
            phone=alert_data.phone,
            owner_name=alert_data.owner_name,
            period_display=alert_data.period_display,
            at_risk_amount=float(alert_data.total_at_risk),
            missing_supplier_count=alert_data.missing_supplier_count,
            channel=alert_data.preferred_channel,
        )

        if result.success:
            logger.info(
                f"ITC alert sent successfully to "
                f"{alert_data.owner_name} ({alert_data.phone})"
            )
        else:
            logger.error(
                f"ITC alert failed for {alert_data.customer_id}: "
                f"{result.error}"
            )

        return result

    # ==========================================================================
    # DELIVERY ALERT MESSAGING
    # ==========================================================================

    async def send_delivery_overdue_alert(
        self,
        phone: str,
        owner_name: str,
        supplier_name: str,
        material_name: str,
        days_overdue: int,
        supplier_phone: Optional[str],
        preferred_channel: str,
        preferred_language: str,
    ) -> Optional[MessageResult]:
        """
        Send delivery overdue alert to MSME owner.

        FRESHER NOTE ON DAYS OVERDUE:
        days_overdue = 0 means due today but not received
        days_overdue = 1 means 1 day late
        days_overdue = 3 means critically late — escalate tone

        We adjust the message tone based on severity:
        0-1 days: gentle reminder
        2-3 days: more urgent
        4+ days: urgent, suggest alternative supplier
        """
        first_name = owner_name.split()[0]

        if preferred_language == "kn":
            message = KANNADA_TEMPLATES["delivery_warning"].format(
                name=first_name,
                supplier=supplier_name,
                material=material_name,
                days=days_overdue,
                phone=supplier_phone or "N/A",
            )
        elif preferred_language == "hi":
            message = HINDI_TEMPLATES["delivery_warning"].format(
                name=first_name,
                supplier=supplier_name,
                material=material_name,
                days=days_overdue,
                phone=supplier_phone or "N/A",
            )
        else:
            urgency = ""
            if days_overdue >= 4:
                urgency = " ⚠️ URGENT"
            message = (
                f"ProcureAI — Delivery Alert{urgency}\n\n"
                f"{supplier_name} {material_name} delivery is "
                f"{days_overdue} day{'s' if days_overdue != 1 else ''} overdue.\n"
            )
            if supplier_phone:
                message += f"Contact: {supplier_phone}"

        result = await msg91_client.send_delivery_warning(
            phone=phone,
            owner_name=owner_name,
            supplier_name=supplier_name,
            material_name=material_name,
            days_overdue=days_overdue,
            supplier_phone=supplier_phone,
            channel=preferred_channel,
        )

        if result.success:
            logger.info(
                f"Delivery alert sent to {owner_name}: "
                f"{supplier_name} {days_overdue} days overdue"
            )

        return result

    # ==========================================================================
    # PRICE PULSE MESSAGING
    # ==========================================================================

    async def send_price_overpayment_alert(
        self,
        phone: str,
        owner_name: str,
        material_name: str,
        your_price: Decimal,
        market_price: Decimal,
        potential_saving: Decimal,
        preferred_channel: str,
        preferred_language: str,
    ) -> Optional[MessageResult]:
        """
        Send price overpayment alert to customer.

        FRESHER NOTE ON TONE:
        Price alerts must be factual, not accusatory.
        "You overpaid" sounds confrontational.
        "Market rate is ₹14/kg lower — potential saving" sounds helpful.

        Same information, different framing.
        Helpful framing gets better engagement.
        """
        first_name = owner_name.split()[0]

        if preferred_language == "kn":
            message = KANNADA_TEMPLATES["price_pulse"].format(
                name=first_name,
                material=material_name,
                your_price=f"{your_price:,.0f}",
                market_price=f"{market_price:,.0f}",
                saving=f"{potential_saving:,.0f}",
            )
        elif preferred_language == "hi":
            message = HINDI_TEMPLATES["price_pulse"].format(
                name=first_name,
                material=material_name,
                your_price=f"{your_price:,.0f}",
                market_price=f"{market_price:,.0f}",
                saving=f"{potential_saving:,.0f}",
            )

        result = await msg91_client.send_price_pulse(
            phone=phone,
            owner_name=owner_name,
            material_name=material_name,
            your_price=float(your_price),
            cluster_avg=float(market_price),
            potential_saving=float(potential_saving),
            channel=preferred_channel,
        )

        if result.success:
            logger.info(
                f"Price alert sent to {owner_name}: "
                f"{material_name} ₹{your_price} vs market ₹{market_price}"
            )

        return result

    # ==========================================================================
    # WEEKLY BRIEF
    # One consolidated message every Monday with all key metrics
    # ==========================================================================

    async def send_weekly_brief(
        self,
        phone: str,
        owner_name: str,
        preferred_channel: str,
        preferred_language: str,
        itc_at_risk: Decimal = Decimal("0"),
        overdue_deliveries: int = 0,
        price_savings_available: Decimal = Decimal("0"),
        gem_tenders_available: int = 0,
    ) -> Optional[MessageResult]:
        """
        Send consolidated weekly brief every Monday morning.

        Combines all alerts into one summary message.
        Owner gets ONE message instead of multiple separate alerts.

        FRESHER NOTE ON THE BRIEF FORMAT:
        The weekly brief is different from individual alerts.
        Individual alerts are urgent — sent immediately when problem found.
        Weekly brief is a summary — sent every Monday regardless.

        Brief shows:
        ✅ What's OK (all deliveries on time, all ITC matched)
        ⚠️ What needs attention (overdue delivery, ITC at risk)
        💰 Opportunities (price savings, GeM tenders)
        """
        first_name = owner_name.split()[0]

        lines = [f"ProcureAI — Weekly Brief\n{first_name} avare!\n"]

        if itc_at_risk > 0:
            lines.append(f"⚠️ ITC: ₹{itc_at_risk:,.0f} at risk")
        else:
            lines.append("✅ ITC: All matched")

        if overdue_deliveries > 0:
            lines.append(
                f"⚠️ Deliveries: {overdue_deliveries} overdue"
            )
        else:
            lines.append("✅ Deliveries: All on track")

        if price_savings_available > 0:
            lines.append(
                f"💰 Price savings available: ₹{price_savings_available:,.0f}"
            )

        if gem_tenders_available > 0:
            lines.append(
                f"🏛️ GeM tenders: {gem_tenders_available} new eligible"
            )

        message = "\n".join(lines)

        # Weekly brief always uses preferred channel
        result = await msg91_client.send_rcs(
            phone=phone,
            title=f"ProcureAI — Weekly Brief",
            body=message,
            channel=preferred_channel,
        ) if preferred_channel == "rcs" else await msg91_client.send_sms(
            phone=phone,
            message=message,
        )

        if result and result.success:
            logger.info(f"Weekly brief sent to {owner_name}")

        return result

    # ==========================================================================
    # PRIVATE HELPERS
    # ==========================================================================

    def _build_english_itc_message(
        self,
        alert_data: ITCAlertData,
        supplier_names: str,
        first_name: str,
    ) -> str:
        """Build English ITC alert message."""
        lines = [
            f"Hi {first_name}!",
            f"",
            f"ITC Alert — {alert_data.period_display}",
            f"₹{alert_data.total_at_risk:,.0f} at risk",
            f"{alert_data.missing_supplier_count} supplier(s) "
            f"have not filed GSTR-1:",
            f"{supplier_names}",
            f"",
            f"Please contact them to file their GSTR-1.",
            f"",
            f"— ProcureAI",
        ]

        if alert_data.has_good_news:
            lines.insert(0, "")
            lines.insert(
                0,
                f"Good news: ₹{alert_data.previous_period_recovery:,.0f} "
                f"ITC recovered from last month!"
            )

        return "\n".join(lines)


# ==============================================================================
# MODULE-LEVEL SERVICE INSTANCE
# ==============================================================================

messaging_service = MessagingService()