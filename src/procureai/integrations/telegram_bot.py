# ==============================================================================
# PROCUREAI — integrations/telegram_bot.py
# Telegram Bot client for alert delivery and invoice receipt.
#
# FRESHER EXPLANATION:
# Telegram Bot API allows us to:
# 1. SEND messages to customers (alerts, reports, summaries)
# 2. RECEIVE messages from customers (invoice PDFs, commands)
#
# HOW IT WORKS:
# Every Telegram user has a unique chat_id (a number).
# When Ramesh starts our bot, we get his chat_id.
# We store it in customer.telegram_chat_id.
# All future messages go to that chat_id.
#
# TWO MODES:
# Mode 1 — Polling (development): Bot checks for new messages every few seconds
# Mode 2 — Webhook (production): Telegram sends messages to our API endpoint
#
# We use polling for development (simpler, no public URL needed).
# We use webhook for production (Render has a public URL).
# ==============================================================================

import base64
from typing import Optional

import httpx
from loguru import logger

from procureai.config import get_settings

settings = get_settings()

# Telegram Bot API base URL
TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


class TelegramBotClient:
    """
    Client for sending messages via Telegram Bot API.

    FRESHER NOTE:
    Telegram Bot API is completely free.
    No DLT registration. No per-message cost.
    No approval process.
    Just an HTTP API that anybody can call.
    """

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> dict:
        """
        Send a text message to a Telegram chat.

        Args:
            chat_id: Telegram user's chat ID
            text: Message text (supports HTML formatting)
            parse_mode: "HTML" or "Markdown"
            reply_markup: Optional inline keyboard buttons

        FRESHER NOTE ON HTML FORMATTING:
        Telegram supports HTML tags in messages:
        <b>bold</b>
        <i>italic</i>
        <code>monospace</code>
        We use these to make alerts visually clear.
        """
        async with httpx.AsyncClient() as client:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }

            if reply_markup:
                payload["reply_markup"] = reply_markup

            response = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json=payload,
                timeout=10.0,
            )

            if response.status_code == 200:
                logger.info(
                    f"Telegram message sent to chat_id={chat_id}"
                )
                return response.json()
            else:
                logger.error(
                    f"Telegram send failed: {response.status_code} "
                    f"{response.text}"
                )
                return {"ok": False, "error": response.text}

    async def send_itc_alert(
        self,
        chat_id: int,
        owner_name: str,
        period_display: str,
        at_risk_amount: float,
        missing_supplier_count: int,
        missing_suppliers: list[dict],
        recovered_amount: float = 0,
    ) -> dict:
        """
        Send ITC at-risk alert via Telegram.

        Formats a rich HTML message with:
        - Good news (recovery) if any
        - ITC at risk amount
        - Which suppliers to contact
        - Action buttons
        """
        first_name = owner_name.split()[0]

        lines = []

        # Good news first if recovery happened
        if recovered_amount > 0:
            lines.append(
                f"🎉 <b>Shubha Samachar!</b>\n"
                f"₹{recovered_amount:,.0f} ITC recover aayitu!"
            )
            lines.append("")

        # ITC alert
        lines.append(f"🏭 <b>ProcureAI — ITC Alert</b>")
        lines.append(f"Namaskara <b>{first_name}</b> avare!")
        lines.append("")
        lines.append(f"📅 <b>{period_display}</b>")
        lines.append(
            f"⚠️ <b>₹{at_risk_amount:,.0f}</b> ITC riskalli ide"
        )
        lines.append(
            f"{missing_supplier_count} supplier(s) "
            f"GSTR-1 file maadilla"
        )
        lines.append("")

        # List missing suppliers
        if missing_suppliers:
            lines.append("<b>Avrige call maadi:</b>")
            for supplier in missing_suppliers[:3]:
                phone = supplier.get("supplier_phone", "N/A")
                name = supplier.get("supplier_name", "Unknown")
                amount = supplier.get("gst_amount", 0)
                lines.append(
                    f"• {name} — ₹{float(amount):,.0f}\n"
                    f"  📞 {phone}"
                )

        lines.append("")
        lines.append("— ProcureAI")

        message = "\n".join(lines)

        # Inline keyboard buttons
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Call maadide",
                        "callback_data": "itc_called"
                    },
                    {
                        "text": "📊 Details nodi",
                        "callback_data": "itc_details"
                    }
                ],
                [
                    {
                        "text": "❌ Dismiss",
                        "callback_data": "itc_dismiss"
                    }
                ]
            ]
        }

        return await self.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
        )

    async def send_delivery_warning(
        self,
        chat_id: int,
        owner_name: str,
        supplier_name: str,
        material_name: str,
        days_overdue: int,
        supplier_phone: Optional[str] = None,
    ) -> dict:
        """Send delivery overdue alert via Telegram."""
        first_name = owner_name.split()[0]

        if days_overdue == 0:
            status = "ee dina due aagide — confirm maadilla"
        elif days_overdue == 1:
            status = "1 dina late aagide"
        else:
            status = f"{days_overdue} dina late aagide"

        lines = [
            "🚚 <b>ProcureAI — Delivery Alert</b>",
            f"Namaskara <b>{first_name}</b> avare!",
            "",
            f"⚠️ <b>{supplier_name}</b>",
            f"{material_name} delivery <b>{status}</b>",
        ]

        if supplier_phone:
            lines.append(f"📞 Call maadi: <code>{supplier_phone}</code>")

        lines.append("")
        lines.append("— ProcureAI")

        message = "\n".join(lines)

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Received aayitu",
                        "callback_data": f"delivery_received"
                    },
                    {
                        "text": "📞 Call maadide",
                        "callback_data": "delivery_called"
                    }
                ]
            ]
        }

        return await self.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
        )

    async def send_price_pulse(
        self,
        chat_id: int,
        owner_name: str,
        material_name: str,
        your_price: float,
        market_price: float,
        potential_saving: float,
        difference_pct: float,
    ) -> dict:
        """Send weekly price overpayment alert via Telegram."""
        first_name = owner_name.split()[0]

        lines = [
            "📊 <b>ProcureAI — Price Pulse</b>",
            f"Namaskara <b>{first_name}</b> avare!",
            "",
            f"<b>{material_name}</b>:",
            f"💰 Neevu kotiddu: <b>₹{your_price:,.0f}/kg</b>",
            f"📈 Market rate: <b>₹{market_price:,.0f}/kg</b>",
            f"📉 Neevu {difference_pct:.1f}% jaasti kotidira",
            "",
            f"💡 Next order li ulita: "
            f"<b>₹{potential_saving:,.0f}</b>",
            "",
            "— ProcureAI"
        ]

        message = "\n".join(lines)

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔍 Better price hudi",
                        "callback_data": f"price_find_{material_name}"
                    },
                    {
                        "text": "❌ Dismiss",
                        "callback_data": "price_dismiss"
                    }
                ]
            ]
        }

        return await self.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
        )

    async def send_morning_brief(
        self,
        chat_id: int,
        owner_name: str,
        itc_at_risk: float = 0,
        overdue_deliveries: int = 0,
        price_savings: float = 0,
        overdue_payments: float = 0,
    ) -> dict:
        """
        Send daily morning brief via Telegram.
        One message with everything he needs to start his day.
        """
        first_name = owner_name.split()[0]

        from datetime import date
        today = date.today().strftime("%d %B %Y")

        lines = [
            f"☀️ <b>Good Morning, {first_name} avare!</b>",
            f"📅 {today}",
            "",
            "<b>Iyya summary:</b>",
            "",
        ]

        # ITC status
        if itc_at_risk > 0:
            lines.append(
                f"⚠️ ITC: ₹{itc_at_risk:,.0f} riskalli ide"
            )
        else:
            lines.append("✅ ITC: Ella sari ide")

        # Deliveries
        if overdue_deliveries > 0:
            lines.append(
                f"⚠️ Deliveries: {overdue_deliveries} late aagide"
            )
        else:
            lines.append("✅ Deliveries: Ella on track ide")

        # Price savings
        if price_savings > 0:
            lines.append(
                f"💰 Price savings: ₹{price_savings:,.0f} available"
            )

        # Overdue payments
        if overdue_payments > 0:
            lines.append(
                f"💵 Customers: ₹{overdue_payments:,.0f} overdue"
            )

        lines.append("")
        lines.append("Have a productive day! 🏭")
        lines.append("— ProcureAI")

        message = "\n".join(lines)

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 Full details",
                        "callback_data": "brief_details"
                    },
                    {
                        "text": "📦 Deliveries",
                        "callback_data": "brief_deliveries"
                    }
                ]
            ]
        }

        return await self.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
        )

    async def send_welcome(self, chat_id: int) -> dict:
        """
        Send welcome message when customer first starts the bot.
        """
        message = (
            "🏭 <b>ProcureAI ge swagata!</b>\n\n"
            "Naanu ninna procurement assistant.\n\n"
            "<b>Naan maaduvudu:</b>\n"
            "✅ ITC leakage track maadtini\n"
            "✅ Supplier delivery follow-up maadtini\n"
            "✅ MS Rod, Steel rate check maadtini\n"
            "✅ Every morning summary kaltini\n\n"
            "<b>Commands:</b>\n"
            "/itc — ITC status nodi\n"
            "/delivery — Pending deliveries nodi\n"
            "/price — Market rates nodi\n"
            "/help — Help beku\n\n"
            "Invoice PDF iddare — illi forward maadi!\n\n"
            "— ProcureAI Team"
        )

        return await self.send_message(
            chat_id=chat_id,
            text=message,
        )

    async def get_updates(self, offset: int = 0) -> list:
        """
        Get new messages from Telegram (polling mode).
        Used in development — production uses webhooks.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"]
                },
                timeout=35.0,
            )
            data = response.json()
            if data.get("ok"):
                return data.get("result", [])
            return []

    async def set_webhook(self, webhook_url: str) -> dict:
        """
        Set webhook URL for production.
        Telegram will POST updates to this URL.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELEGRAM_API}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                }
            )
            result = response.json()
            if result.get("ok"):
                logger.info(f"Webhook set: {webhook_url}")
            else:
                logger.error(f"Webhook failed: {result}")
            return result

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> dict:
        """
        Answer a callback query (button tap).
        Required to remove the loading indicator on the button.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                }
            )
            return response.json()

    async def handle_document(
        self,
        file_id: str,
    ) -> Optional[bytes]:
        """
        Download a document (PDF invoice) sent by a customer.
        Returns the file bytes for processing.
        """
        async with httpx.AsyncClient() as client:
            # Get file path
            response = await client.get(
                f"{TELEGRAM_API}/getFile",
                params={"file_id": file_id},
            )
            data = response.json()

            if not data.get("ok"):
                logger.error(f"Failed to get file info: {data}")
                return None

            file_path = data["result"]["file_path"]

            # Download file
            token = settings.telegram_bot_token
            file_response = await client.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=30.0,
            )

            if file_response.status_code == 200:
                logger.info(
                    f"Downloaded file: {file_path} "
                    f"({len(file_response.content)} bytes)"
                )
                return file_response.content

            logger.error(
                f"File download failed: {file_response.status_code}"
            )
            return None


# Module-level instance
telegram_client = TelegramBotClient()
