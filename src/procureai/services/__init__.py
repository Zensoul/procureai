
# ==============================================================================
# PROCUREAI — services/__init__.py
# Exports all service instances from a single import point.
#
# FRESHER NOTE:
# Same pattern as models, schemas, and integrations.
# Instead of:
#   from procureai.services.itc_reconciliation import itc_service
#   from procureai.services.delivery_tracker import delivery_service
#   from procureai.services.price_engine import price_service
#   from procureai.services.messaging import messaging_service
#
# You write:
#   from procureai.services import itc_service, delivery_service
#
# Routers and Celery tasks import from here.
# ==============================================================================

from procureai.services.itc_reconciliation import (
    ITCReconciliationService,
    itc_service,
    tax_period_to_display,
    get_previous_period,
)

from procureai.services.delivery_tracker import (
    DeliveryTrackerService,
    delivery_service,
    FOLLOWUP_DAYS_BEFORE,
    ALERT_DAYS_OVERDUE,
)

from procureai.services.price_engine import (
    PriceEngineService,
    price_service,
    ALERT_THRESHOLD_PCT,
    MIN_CLUSTER_SAMPLES,
)

from procureai.services.messaging import (
    MessagingService,
    messaging_service,
    KANNADA_TEMPLATES,
    HINDI_TEMPLATES,
)

__all__ = [
    # ITC
    "ITCReconciliationService",
    "itc_service",
    "tax_period_to_display",
    "get_previous_period",
    # Delivery
    "DeliveryTrackerService",
    "delivery_service",
    "FOLLOWUP_DAYS_BEFORE",
    "ALERT_DAYS_OVERDUE",
    # Price
    "PriceEngineService",
    "price_service",
    "ALERT_THRESHOLD_PCT",
    "MIN_CLUSTER_SAMPLES",
    # Messaging
    "MessagingService",
    "messaging_service",
    "KANNADA_TEMPLATES",
    "HINDI_TEMPLATES",
]