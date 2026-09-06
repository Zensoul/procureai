
# ==============================================================================
# PROCUREAI — main.py
# FastAPI application entry point.
#
# FRESHER EXPLANATION:
# This file is the "front door" of ProcureAI.
# Everything starts here:
# - The FastAPI app is created here
# - All routers are registered here
# - Database connects on startup here
# - Sentry error tracking starts here
# - Every HTTP request enters through here
#
# WHEN YOU RUN:
# uv run uvicorn procureai.main:app --reload
#
# Python finds this file, imports `app`, and starts
# an HTTP server on http://localhost:8000
#
# API DOCUMENTATION:
# FastAPI auto-generates interactive docs at:
# http://localhost:8000/docs  (Swagger UI)
# http://localhost:8000/redoc (ReDoc)
# ==============================================================================

from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from procureai.config import get_settings
from procureai.database import connect_db, disconnect_db
from procureai.routers import customers, internal, purchases, payments, sales

settings = get_settings()


# ==============================================================================
# SENTRY INITIALISATION
# Error tracking — captures exceptions and sends alerts.
# Only enabled in production (SENTRY_DSN must be set).
#
# FRESHER NOTE ON SENTRY:
# Sentry watches your application for unhandled exceptions.
# When something crashes in production at 2 AM, Sentry
# sends you an email with the full stack trace, the request
# that caused it, and which line of code failed.
# Without Sentry, you'd only find out when Ramesh calls
# you the next morning saying "the app is broken."
# ==============================================================================

if settings.sentry_dsn and settings.is_production:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        environment=settings.app_env,
        # Don't send personally identifiable information
        send_default_pii=False,
    )
    logger.info("Sentry error tracking enabled")


# ==============================================================================
# LIFESPAN — STARTUP AND SHUTDOWN
#
# FRESHER NOTE ON LIFESPAN:
# The lifespan context manager runs code at two points:
# 1. BEFORE the app starts accepting requests (startup)
# 2. AFTER the app stops accepting requests (shutdown)
#
# We use startup to:
# - Connect to the database (verify it's reachable)
# - Log that the app started
#
# We use shutdown to:
# - Close database connections cleanly
# - Prevent "connection already closed" errors in logs
#
# @asynccontextmanager turns a generator function into
# a context manager. Everything before `yield` is startup.
# Everything after `yield` is shutdown.
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown logic."""

    # ── STARTUP ───────────────────────────────────────────────────────────────
    logger.info(
        f"Starting {settings.app_name} "
        f"[{settings.app_env}]"
    )

    # Connect to database — raises RuntimeError if unreachable
    await connect_db()

    # Verify Redis is reachable
    try:
        from redis import Redis
        r = Redis.from_url(settings.redis_url, ssl_cert_reqs=None)
        r.ping()
        r.close()
        logger.info("Redis connection verified")
    except Exception as e:
        logger.warning(f"Redis not reachable at startup: {e}")
        # Don't crash on Redis failure — app can still serve
        # API requests even if background jobs are unavailable

    logger.info(
        f"{settings.app_name} started successfully. "
        f"API docs: http://localhost:8000/docs"
    )

    yield  # App runs here — handling requests

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info(f"Shutting down {settings.app_name}...")
    await disconnect_db()
    logger.info("Shutdown complete")


# ==============================================================================
# FASTAPI APPLICATION
#
# FRESHER NOTE ON FastAPI PARAMETERS:
# title: shown in API docs
# description: shown in API docs
# version: API version number
# lifespan: the startup/shutdown function above
# docs_url: where Swagger UI lives (disable in production if needed)
# redoc_url: where ReDoc lives
# ==============================================================================

app = FastAPI(
    title=settings.app_name,
    description="""
## ProcureAI — MSME Procurement Intelligence

AI-powered procurement intelligence for Indian manufacturing MSMEs.

### Features
- **ITC Recovery**: Monthly GSTR-2B reconciliation and at-risk ITC alerts
- **Delivery Tracking**: Automated supplier follow-ups and overdue alerts
- **Price Intelligence**: Weekly benchmarking against cluster market rates

### Authentication
- Customer/CA endpoints: Bearer JWT token
- Internal endpoints: Bearer service token (SECRET_KEY)
    """,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ==============================================================================
# MIDDLEWARE
#
# Middleware runs on EVERY request — before and after your route handler.
# Think of it as a pipeline: request → middleware → route → middleware → response
#
# FRESHER NOTE ON CORS:
# CORS (Cross-Origin Resource Sharing) controls which websites
# can call your API from a browser.
# Without CORS headers, the CA's dashboard (running on port 3000)
# cannot make API calls to your server (running on port 8000).
# The browser blocks it as a security measure.
#
# allow_origins=["*"] in development — allow all origins.
# In production, restrict to your actual frontend URL.
# ==============================================================================

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_development else [
        "https://your-ca-dashboard.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==============================================================================
# GLOBAL EXCEPTION HANDLERS
#
# These catch unhandled exceptions and return clean JSON responses
# instead of ugly Python tracebacks.
#
# FRESHER NOTE:
# Without exception handlers, if something crashes inside a route,
# FastAPI returns a 500 error with a Python traceback in HTML.
# That exposes your code structure and is ugly.
# With handlers, every error returns clean JSON:
# {"detail": "Internal server error", "error_id": "abc-123"}
# ==============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch all unhandled exceptions.
    Log them, report to Sentry, return clean JSON.
    """
    logger.error(
        f"Unhandled exception on {request.method} {request.url}: "
        f"{type(exc).__name__}: {exc}"
    )

    # Sentry captures it automatically if configured
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error. "
                      "Our team has been notified.",
        }
    )

@app.exception_handler(ValueError)
async def value_error_handler(
    request: Request, exc: ValueError
) -> JSONResponse:
    """Convert ValueError to 422 response."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc)},
    )


# ==============================================================================
# ROUTERS
# Register all routers with the app.
# Each router handles a group of related endpoints.
#
# FRESHER NOTE ON INCLUDE_ROUTER:
# app.include_router(router) attaches all routes from that router
# to the main app. The router's prefix is preserved.
#
# After these three lines, the app has:
# /customers/* routes (from customers router)
# /purchases/* routes (from purchases router)
# /internal/* routes (from internal router)
# ==============================================================================

app.include_router(customers.router)
app.include_router(purchases.router)
app.include_router(payments.router)
app.include_router(internal.router)
app.include_router(sales.router)


# ==============================================================================
# ROOT ENDPOINT
# ==============================================================================

@app.get("/", tags=["root"])
async def root() -> dict:
    """
    Root endpoint — confirms the API is running.
    Used by load balancers and simple connectivity checks.
    """
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "status": "running",
        "environment": settings.app_env,
        "docs": "/docs",
    }


# ==============================================================================
# ENTRY POINT
# When running directly: python -m procureai.main
# Usually run via: uv run uvicorn procureai.main:app --reload
# ==============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "procureai.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.is_development,
        log_level=settings.log_level.lower(),
    )