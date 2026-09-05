
# ==============================================================================
# PROCUREAI — celery_app.py
# Celery application configuration and task definitions.
#
# FRESHER EXPLANATION:
# Celery is a task queue system. Think of it like a post office:
#
# Your FastAPI app = person who writes letters (creates tasks)
# Redis = the post office (stores tasks in a queue)
# Celery worker = postman (picks up tasks and delivers them)
#
# WHY THIS IS BETTER THAN APScheduler:
# With APScheduler, if FastAPI crashes mid-task, the task is lost.
# With Celery + Redis:
# - Task is stored in Redis before execution starts
# - If worker crashes, task stays in Redis and retries
# - Multiple workers can run tasks in parallel
# - Flower dashboard shows task history, failures, timing
#
# HOW IT WORKS IN PROCUREAI:
# 1. n8n or Railway Cron triggers POST /internal/run-itc-monthly
# 2. FastAPI creates a Celery task and puts it in Redis
# 3. Celery worker picks up the task from Redis
# 4. Worker runs ITC reconciliation for all customers
# 5. Results stored back in Redis
# 6. FastAPI can check task status if needed
#
# THREE SCHEDULED TASKS:
# - run_itc_monthly: 16th of every month, 9 AM
# - run_delivery_check: Every day, 8 AM
# - run_price_pulse: Every Monday, 9 AM
# ==============================================================================

from celery import Celery
from celery.schedules import crontab
from loguru import logger

from procureai.config import get_settings

settings = get_settings()

# ==============================================================================
# CELERY APPLICATION
#
# broker: Where tasks are sent (Redis queue)
# backend: Where task results are stored (Redis)
# Using the same Redis for both is fine at our scale.
# ==============================================================================

celery_app = Celery(
    "procureai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        # Tell Celery where to find task definitions
        "procureai.tasks.itc_tasks",
        "procureai.tasks.delivery_tasks",
        "procureai.tasks.price_tasks",
    ]
)

# ==============================================================================
# CELERY CONFIGURATION
# ==============================================================================

celery_app.conf.update(
    # Serialization — use JSON for tasks (readable, debuggable)
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone — always UTC internally, display IST to users
    timezone="UTC",
    enable_utc=True,

    # Task result expiry — keep results for 24 hours
    result_expires=86400,

    # Retry failed tasks up to 3 times
    task_max_retries=3,

    # Wait between retries: 60s, 120s, 240s (exponential)
    task_default_retry_delay=60,

    # Acknowledge task only after completion
    # If worker crashes mid-task, task goes back to queue
    task_acks_late=True,

    # Worker settings
    worker_prefetch_multiplier=1,  # Process one task at a time per worker
    worker_max_tasks_per_child=100,  # Restart worker after 100 tasks (memory leak prevention)
)

# ==============================================================================
# BEAT SCHEDULE
# Celery Beat is the scheduler — like cron but integrated with Celery.
# These schedules tell Celery Beat when to create tasks automatically.
#
# FRESHER NOTE ON CRONTAB:
# crontab(day_of_month=16, hour=3, minute=30) means:
# "Run on the 16th of every month at 3:30 AM UTC"
# 3:30 AM UTC = 9:00 AM IST (UTC+5:30)
# Always schedule in UTC, display in IST.
# ==============================================================================

celery_app.conf.beat_schedule = {
    # Feature 1 — ITC Monthly Reconciliation
    # GSTR-2B is published on 14th. We run on 16th to ensure data is ready.
    # 3:30 AM UTC = 9:00 AM IST
    "itc-monthly-reconciliation": {
        "task": "procureai.tasks.itc_tasks.run_itc_batch",
        "schedule": crontab(day_of_month=16, hour=3, minute=30),
        "kwargs": {},
    },

    # Feature 2 — Daily Delivery Check
    # Check all pending deliveries every morning.
    # 2:30 AM UTC = 8:00 AM IST
    "delivery-daily-check": {
        "task": "procureai.tasks.delivery_tasks.run_delivery_check",
        "schedule": crontab(hour=2, minute=30),
        "kwargs": {},
    },

    # Feature 2b — Supplier Follow-up
    # Send follow-up to suppliers 48 hours before expected delivery.
    # Runs twice daily to catch morning and afternoon orders.
    # 4:00 AM UTC = 9:30 AM IST
    "supplier-followup-morning": {
        "task": "procureai.tasks.delivery_tasks.run_supplier_followup",
        "schedule": crontab(hour=4, minute=0),
        "kwargs": {},
    },

    # Feature 3 — Weekly Price Pulse
    # Every Monday morning — sets the week's price benchmark.
    # 3:30 AM UTC Monday = 9:00 AM IST Monday
    "weekly-price-pulse": {
        "task": "procureai.tasks.price_tasks.run_price_pulse",
        "schedule": crontab(day_of_week=1, hour=3, minute=30),
        "kwargs": {},
    },
}