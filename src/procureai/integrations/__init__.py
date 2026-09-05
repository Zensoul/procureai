
# ==============================================================================
# PROCUREAI — integrations/__init__.py
# Exports all external API clients from a single import point.
#
# FRESHER NOTE:
# Same pattern as models and schemas.
# Instead of:
#   from procureai.integrations.sandbox_gst import gst_client
#   from procureai.integrations.msg91 import msg91_client
#   from procureai.integrations.mcx import mcx_client
#
# You write:
#   from procureai.integrations import gst_client, msg91_client, mcx_client
#
# The module-level client instances are singletons —
# created once when the module loads, reused everywhere.
# ==============================================================================

from procureai.integrations.sandbox_gst import (
    SandboxGSTClient,
    gst_client,
    GSTR2BData,
    GSTR2BInvoice,
    GSTNAPIError,
    InvalidGSTINError,
    GSTNAuthError,
    GSTNDataNotAvailableError,
    GSTNRateLimitError,
)

from procureai.integrations.msg91 import (
    MSG91Client,
    msg91_client,
    MessageType,
    MessageResult,
    RCSButton,
    MSG91Error,
    MSG91AuthError,
    MSG91DeliveryError,
)

from procureai.integrations.mcx import (
    MCXPriceClient,
    mcx_client,
    CommodityPrice,
    PriceComparison,
)

__all__ = [
    # GST
    "SandboxGSTClient",
    "gst_client",
    "GSTR2BData",
    "GSTR2BInvoice",
    "GSTNAPIError",
    "InvalidGSTINError",
    "GSTNAuthError",
    "GSTNDataNotAvailableError",
    "GSTNRateLimitError",
    # Messaging
    "MSG91Client",
    "msg91_client",
    "MessageType",
    "MessageResult",
    "RCSButton",
    "MSG91Error",
    "MSG91AuthError",
    "MSG91DeliveryError",
    # Prices
    "MCXPriceClient",
    "mcx_client",
    "CommodityPrice",
    "PriceComparison",
]