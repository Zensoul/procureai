
# ==============================================================================
# PROCUREAI — schemas/__init__.py
# Exports all schemas from a single import point.
#
# FRESHER NOTE:
# Same principle as models/__init__.py.
# Instead of writing:
#   from procureai.schemas.customer import CustomerCreate
#   from procureai.schemas.purchase import PurchaseCreate
#   from procureai.schemas.itc import ITCReconciliationRequest
#
# You write:
#   from procureai.schemas import CustomerCreate, PurchaseCreate
#
# Every new schema file must be imported here.
# ==============================================================================

from procureai.schemas.customer import (
    CustomerCreate,
    CustomerUpdate,
    CustomerResponse,
    CustomerListResponse,
)

from procureai.schemas.purchase import (
    PurchaseCreate,
    PurchaseUpdate,
    PurchaseResponse,
    PurchaseSummary,
    MissingGSTR2BItem,
)

from procureai.schemas.itc import (
    ITCReconciliationRequest,
    ITCBatchReconciliationRequest,
    ITCReconciliationResult,
    ITCSupplierBreakdown,
    ITCFeeCalculation,
    ITCAlertData,
    ITCTrackingResponse,
    ITCMonthlyReport,
)

__all__ = [
    # Customer schemas
    "CustomerCreate",
    "CustomerUpdate",
    "CustomerResponse",
    "CustomerListResponse",
    # Purchase schemas
    "PurchaseCreate",
    "PurchaseUpdate",
    "PurchaseResponse",
    "PurchaseSummary",
    "MissingGSTR2BItem",
    # ITC schemas
    "ITCReconciliationRequest",
    "ITCBatchReconciliationRequest",
    "ITCReconciliationResult",
    "ITCSupplierBreakdown",
    "ITCFeeCalculation",
    "ITCAlertData",
    "ITCTrackingResponse",
    "ITCMonthlyReport",
]