# ==============================================================================
# PROCUREAI — integrations/msg91.py
# MSG91 client for RCS and SMS message delivery.
#
# FRESHER EXPLANATION:
# This file is responsible for actually sending messages to Ramesh's phone.
# Every alert in ProcureAI — ITC warning, delivery follow-up, price pulse —
# passes through this file before reaching the customer.
#
# WHY MSG91:
# 1. Indian company — understands DLT compliance requirements
# 2. Supports RCS natively with automatic SMS fallback
# 3. Reliable delivery to all Indian carriers (Jio, Airtel, Vi)
# 4. Competitive pricing — ₹0.10-0.25 per message
#
# MESSAGE TYPES WE SEND:
# 1. ITC Alert — "₹59,000 ITC at risk from 2 suppliers"
# 2. Delivery Warning — "Sharma Metals delivery overdue by 2 days"
# 3. Price Pulse — "You paid ₹14/kg above market for MS Rod"
# 4. Supplier Follow-up — automated message TO the supplier
#
# DLT COMPLIANCE:
# Every commercial message in India must be registered with TRAI's
# DLT (Distributed Ledger Technology) platform.
# Each message template has a DLT template ID.
# Sending without DLT registration = messages blocked by carriers.
# ==============================================================================

from enum import Enum
from typing import Optional

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from procureai.config import get_settings

settings = get_settings()


# ==============================================================================
# MESSAGE TYPES
# ==============================================================================

class MessageType(str, Enum):
    """
    Types of messages ProcureAI sends.

    FRESHER NOTE ON ENUM:
    An Enum is a set of named constants.
    Instead of using raw strings like "itc_alert" everywhere
    (easy to typo as "itc_Alert" or "ITC_alert"),
    we use MessageType.ITC_ALERT — Python catches typos at
    import time, not at 2 AM when the alert fails.
    """
    ITC_ALERT = "itc_alert"
    DELIVERY_WARNING = "delivery_warning"
    PRICE_PULSE = "price_pulse"
    SUPPLIER_FOLLOWUP = "supplier_followup"
    WEEKLY_BRIEF = "weekly_brief"
    OTP = "otp"


class DeliveryStatus(str, Enum):
    """Status of a sent message."""
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    PENDING = "pending"


# ==============================================================================
# CUSTOM EXCEPTIONS
# ==============================================================================

class MSG91Error(Exception):
    """Base exception for MSG91 errors."""
    pass

class MSG91AuthError(MSG91Error):
    """Invalid API key."""
    pass

class MSG91DeliveryError(MSG91Error):
    """Message could not be delivered."""
    pass

class MSG91RateLimitError(MSG91Error):
    """Too many requests."""
    pass


# ==============================================================================
# MESSAGE RESULT
# What the client returns after sending a message
# ==============================================================================

class MessageResult:
    """
    Result of a send operation.

    FRESHER NOTE:
    Instead of returning True/False, we return a rich object
    with the message ID, delivery status, and channel used.
    This lets us log exactly what happened and debug delivery issues.
    """

    def __init__(
        self,
        success: bool,
        message_id: Optional[str],
        channel_used: str,
        error: Optional[str] = None,
    ):
        self.success = success
        self.message_id = message_id
        self.channel_used = channel_used
        self.error = error

    def __repr__(self) -> str:
        return (
            f"MessageResult("
            f"success={self.success}, "
            f"channel={self.channel_used}, "
            f"id={self.message_id}"
            f")"
        )


# ==============================================================================
# RCS BUTTON TYPES
# Buttons that appear on RCS rich cards
# ==============================================================================

class RCSButton:
    """
    A button on an RCS rich card.

    FRESHER NOTE:
    RCS supports interactive buttons — unlike SMS which is plain text.
    When Ramesh receives an ITC alert as an RCS card, he sees:
    [Check Details] [Call Supplier] [Dismiss]
    Each button sends a postback to our webhook when tapped.
    """

    def __init__(self, text: str, postback_data: str):
        self.text = text
        self.postback_data = postback_data

    def to_dict(self) -> dict:
        return {
            "reply": {
                "text": self.text,
                "postbackData": self.postback_data,
            }
        }


# ==============================================================================
# MSG91 CLIENT
# ==============================================================================

class MSG91Client:
    """
    HTTP client for MSG91 RCS and SMS API.

    FRESHER NOTE ON CLASS DESIGN:
    We put all MSG91 communication in one class so:
    1. API key and base URL are configured once
    2. Error handling logic is in one place
    3. Retry logic is in one place
    4. Easy to swap MSG91 for another provider later
       (just replace this class, nothing else changes)
    """

    def __init__(self):
        self.api_key = settings.msg91_api_key
        self.sender_id = settings.msg91_sender_id
        self.base_url = settings.msg91_base_url

    def _get_headers(self) -> dict:
        """Standard headers for MSG91 API requests."""
        return {
            "authkey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _is_configured(self) -> bool:
        """
        Check if MSG91 is properly configured.
        Returns False in development when using placeholder keys.
        """
        return self.api_key != "your-msg91-key"

    # ==========================================================================
    # CORE SEND METHODS
    # ==========================================================================

    @retry(
        retry=retry_if_exception_type((
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            MSG91RateLimitError,
        )),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
    )
    async def send_sms(
        self,
        phone: str,
        message: str,
        message_type: MessageType = MessageType.ITC_ALERT,
    ) -> MessageResult:
        """
        Send a plain SMS message.

        Used as:
        1. Primary channel when customer prefers SMS
        2. Automatic fallback when RCS fails
        3. For customers on basic phones without RCS

        Args:
            phone: Indian mobile number (10 digits)
            message: Plain text message content
            message_type: Type of message for logging

        FRESHER NOTE ON DLT:
        In production, SMS content must match a pre-registered
        DLT template exactly. During development we bypass this.
        Register your templates at msg91.com before going live.
        """
        # Development mode — log instead of send
        if not self._is_configured():
            logger.info(
                f"[MOCK SMS] To: {phone} | "
                f"Type: {message_type} | "
                f"Message: {message[:50]}..."
            )
            return MessageResult(
                success=True,
                message_id=f"mock_sms_{phone}",
                channel_used="sms_mock",
            )

        async with httpx.AsyncClient() as client:
            try:
                payload = {
                    "sender": self.sender_id,
                    "route": "4",  # Transactional route
                    "country": "91",
                    "sms": [{
                        "message": message,
                        "to": [phone],
                    }]
                }

                response = await client.post(
                    f"{self.base_url}/sendhttp.php",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=10.0,
                )

                return self._handle_sms_response(response, phone)

            except httpx.TimeoutException:
                logger.error(f"MSG91 SMS timeout for {phone}")
                raise

    @retry(
        retry=retry_if_exception_type((
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            MSG91RateLimitError,
        )),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
    )
    async def send_rcs(
        self,
        phone: str,
        title: str,
        body: str,
        buttons: Optional[list[RCSButton]] = None,
        message_type: MessageType = MessageType.ITC_ALERT,
    ) -> MessageResult:
        """
        Send an RCS rich card message with optional buttons.
        Automatically falls back to SMS if RCS unavailable.

        Args:
            phone: Indian mobile number
            title: Card title shown in bold
            body: Card body text
            buttons: Optional list of action buttons
            message_type: Type for logging

        FRESHER NOTE ON RCS FALLBACK:
        MSG91 handles RCS→SMS fallback automatically.
        If Ramesh's phone or carrier doesn't support RCS,
        MSG91 sends the body as a plain SMS instead.
        We don't need to detect this — it just works.
        """
        # Development mode — log instead of send
        if not self._is_configured():
            logger.info(
                f"[MOCK RCS] To: {phone} | "
                f"Title: {title} | "
                f"Body: {body[:50]}... | "
                f"Buttons: {[b.text for b in (buttons or [])]}"
            )
            return MessageResult(
                success=True,
                message_id=f"mock_rcs_{phone}",
                channel_used="rcs_mock",
            )

        async with httpx.AsyncClient() as client:
            try:
                payload = {
                    "integrated_number": self.sender_id,
                    "content_type": "RCS",
                    "payload": {
                        "type": "card",
                        "card": {
                            "title": title,
                            "description": body,
                            "suggestions": [
                                b.to_dict() for b in (buttons or [])
                            ],
                        },
                        "to": phone,
                        # Fallback SMS content if RCS unavailable
                        "fallback": {
                            "type": "sms",
                            "message": f"{title}\n{body}",
                        }
                    }
                }

                response = await client.post(
                    f"{self.base_url}/rcs/send",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=10.0,
                )

                return self._handle_rcs_response(response, phone)

            except httpx.TimeoutException:
                logger.error(f"MSG91 RCS timeout for {phone}")
                # Fall back to SMS on timeout
                logger.info(f"Falling back to SMS for {phone}")
                return await self.send_sms(phone, f"{title}\n{body}")

    # ==========================================================================
    # ALERT-SPECIFIC METHODS
    # Pre-built message formats for each alert type.
    # These encode the exact format of each message type —
    # the service layer just calls send_itc_alert() with data.
    # ==========================================================================

    async def send_itc_alert(
        self,
        phone: str,
        owner_name: str,
        period_display: str,
        at_risk_amount: float,
        missing_supplier_count: int,
        channel: str = "rcs",
    ) -> MessageResult:
        """
        Send ITC at-risk alert to customer.

        Example RCS card:
        ┌─────────────────────────────────┐
        │ ProcureAI — ITC Alert           │
        │ August 2026                     │
        │                                 │
        │ ₹59,000 ITC at risk             │
        │ 2 suppliers have not filed      │
        │                                 │
        │ [Check Details] [Dismiss]       │
        └─────────────────────────────────┘
        """
        title = f"ProcureAI — ITC Alert | {period_display}"
        body = (
            f"Namaskara {owner_name.split()[0]}!\n\n"
            f"₹{at_risk_amount:,.0f} ITC at risk\n"
            f"{missing_supplier_count} supplier"
            f"{'s have' if missing_supplier_count > 1 else ' has'} "
            f"not filed GSTR-1\n\n"
            f"Reply CHECK to see which suppliers"
        )

        logger.info(
            f"Sending ITC alert to {phone}: "
            f"₹{at_risk_amount:,.0f} at risk"
        )

        if channel == "rcs":
            return await self.send_rcs(
                phone=phone,
                title=title,
                body=body,
                buttons=[
                    RCSButton("Check Details", "itc_details"),
                    RCSButton("Dismiss", "itc_dismiss"),
                ],
                message_type=MessageType.ITC_ALERT,
            )
        else:
            return await self.send_sms(
                phone=phone,
                message=f"{title}\n{body}",
                message_type=MessageType.ITC_ALERT,
            )

    async def send_delivery_warning(
        self,
        phone: str,
        owner_name: str,
        supplier_name: str,
        material_name: str,
        days_overdue: int,
        supplier_phone: Optional[str] = None,
        channel: str = "rcs",
    ) -> MessageResult:
        """
        Send delivery overdue warning to customer.

        Example:
        Sharma Metals MS Rod delivery is 2 days overdue.
        Contact: 9845XXXXXX
        """
        title = f"ProcureAI — Delivery Alert"
        body = (
            f"{supplier_name} {material_name} delivery "
            f"is {days_overdue} day"
            f"{'s' if days_overdue > 1 else ''} overdue.\n"
        )
        if supplier_phone:
            body += f"Contact: {supplier_phone}"

        logger.info(
            f"Sending delivery warning to {phone}: "
            f"{supplier_name} overdue by {days_overdue} days"
        )

        buttons = [RCSButton("Call Supplier", f"call_{supplier_phone or ''}")]
        if supplier_phone:
            buttons.append(RCSButton("Mark Received", "delivery_received"))

        if channel == "rcs":
            return await self.send_rcs(
                phone=phone,
                title=title,
                body=body,
                buttons=buttons,
                message_type=MessageType.DELIVERY_WARNING,
            )
        else:
            return await self.send_sms(
                phone=phone,
                message=f"{title}\n{body}",
                message_type=MessageType.DELIVERY_WARNING,
            )

    async def send_price_pulse(
        self,
        phone: str,
        owner_name: str,
        material_name: str,
        your_price: float,
        cluster_avg: float,
        potential_saving: float,
        channel: str = "rcs",
    ) -> MessageResult:
        """
        Send weekly price overpayment alert.

        Example:
        MS Rod: You paid ₹188/kg | Market: ₹174/kg
        Potential saving on next order: ₹4,200
        """
        overpayment_pct = (
            (your_price - cluster_avg) / cluster_avg * 100
        )

        title = "ProcureAI — Price Pulse"
        body = (
            f"{material_name}:\n"
            f"You paid: ₹{your_price:,.0f}/kg\n"
            f"Market avg: ₹{cluster_avg:,.0f}/kg\n"
            f"You're paying {overpayment_pct:.1f}% above market\n\n"
            f"Potential saving: ₹{potential_saving:,.0f}"
        )

        logger.info(
            f"Sending price pulse to {phone}: "
            f"{material_name} ₹{your_price} vs market ₹{cluster_avg}"
        )

        if channel == "rcs":
            return await self.send_rcs(
                phone=phone,
                title=title,
                body=body,
                buttons=[
                    RCSButton("Find Better Price", "price_find"),
                    RCSButton("Dismiss", "price_dismiss"),
                ],
                message_type=MessageType.PRICE_PULSE,
            )
        else:
            return await self.send_sms(
                phone=phone,
                message=f"{title}\n{body}",
                message_type=MessageType.PRICE_PULSE,
            )

    async def send_supplier_followup(
        self,
        supplier_phone: str,
        supplier_name: str,
        customer_business_name: str,
        material_name: str,
        quantity: float,
        unit: str,
        expected_date: str,
    ) -> MessageResult:
        """
        Send automated follow-up message TO the supplier.

        This is different — we're messaging the SUPPLIER, not Ramesh.
        Sent 48 hours before expected delivery to confirm.

        Example SMS to Sharma Metals:
        "Dear Sharma Metals, this is an automated message from
        Ramesh Auto Parts. Your delivery of 500kg MS Rod is due
        on 2026-09-07. Please confirm delivery. Reply YES to confirm."
        """
        message = (
            f"Dear {supplier_name}, this is an automated message "
            f"from {customer_business_name}. "
            f"Your delivery of {quantity:.0f}{unit} {material_name} "
            f"is expected on {expected_date}. "
            f"Please confirm. Reply YES to confirm delivery."
        )

        logger.info(
            f"Sending supplier follow-up to {supplier_phone}: "
            f"{material_name} due {expected_date}"
        )

        # Supplier follow-up is always SMS — suppliers may not have RCS
        return await self.send_sms(
            phone=supplier_phone,
            message=message,
            message_type=MessageType.SUPPLIER_FOLLOWUP,
        )

    # ==========================================================================
    # RESPONSE HANDLERS
    # ==========================================================================

    def _handle_sms_response(
        self, response: httpx.Response, phone: str
    ) -> MessageResult:
        """Parse MSG91 SMS API response."""
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code == 200 and data.get("type") == "success":
            message_id = data.get("request_id", "unknown")
            logger.info(f"SMS sent successfully to {phone}: id={message_id}")
            return MessageResult(
                success=True,
                message_id=message_id,
                channel_used="sms",
            )

        if response.status_code == 401:
            raise MSG91AuthError("Invalid MSG91 API key")

        if response.status_code == 429:
            raise MSG91RateLimitError("MSG91 rate limit exceeded")

        error = data.get("message", f"HTTP {response.status_code}")
        logger.error(f"MSG91 SMS failed for {phone}: {error}")
        return MessageResult(
            success=False,
            message_id=None,
            channel_used="sms",
            error=error,
        )

    def _handle_rcs_response(
        self, response: httpx.Response, phone: str
    ) -> MessageResult:
        """Parse MSG91 RCS API response."""
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code == 200:
            message_id = data.get("request_id", "unknown")
            channel = data.get("channel", "rcs")
            logger.info(
                f"RCS message sent to {phone}: "
                f"id={message_id}, channel={channel}"
            )
            return MessageResult(
                success=True,
                message_id=message_id,
                channel_used=channel,
            )

        if response.status_code == 401:
            raise MSG91AuthError("Invalid MSG91 API key")

        if response.status_code == 429:
            raise MSG91RateLimitError("MSG91 rate limit exceeded")

        error = data.get("message", f"HTTP {response.status_code}")
        logger.error(f"MSG91 RCS failed for {phone}: {error}")
        return MessageResult(
            success=False,
            message_id=None,
            channel_used="rcs",
            error=error,
        )


# ==============================================================================
# MODULE-LEVEL CLIENT INSTANCE
# ==============================================================================

msg91_client = MSG91Client()
