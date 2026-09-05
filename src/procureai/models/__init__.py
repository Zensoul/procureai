
# ==============================================================================
# PROCUREAI — models/__init__.py
# Exports all models from a single import point.
#
# FRESHER EXPLANATION:
# This file does two jobs:
#
# Job 1 — Convenience imports
# Instead of: from procureai.models.customer import Customer
# You write:  from procureai.models import Customer
#
# Job 2 — Alembic visibility
# Alembic (our migration tool) scans this file to find ALL models.
# If a model is not imported here, Alembic cannot see it and will
# NOT create its database table. This causes silent failures where
# your Python code works fine but the table doesn't exist in PostgreSQL.
#
# RULE: Every new model file you create MUST be imported here.
# ==============================================================================

from procureai.models.customer import Customer
from procureai.models.itc import ITCTracking
from procureai.models.purchase import Purchase

# __all__ explicitly declares what is exported from this package.
# When someone writes "from procureai.models import *" they get
# exactly these three — nothing else, no surprises.
__all__ = [
    "Customer",
    "Purchase",
    "ITCTracking",
]