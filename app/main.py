import asyncio
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.logging_config import configure_logging
from app.middleware.logging import RequestLoggingMiddleware
from app.routers import api
from app.services import core

# ── Custom system-resource metrics ──────────────────────────────────────────
_cpu_gauge = Gauge("process_cpu_percent", "Current process CPU usage percent")
_ram_gauge = Gauge("process_rss_bytes", "Current process RSS memory in bytes")


def _refresh_system_metrics() -> None:
    proc = psutil.Process()
    _cpu_gauge.set(proc.cpu_percent(interval=None))
    _ram_gauge.set(proc.memory_info().rss)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await core.init_db()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, core.get_classifier)
    # Seed the CPU gauge so the first scrape isn't zero
    proc = psutil.Process()
    proc.cpu_percent(interval=None)
    yield
    await core.close_db_pool()


app = FastAPI(
    lifespan=lifespan,
    title="Medical Symptom Classifier API",
    description="""
## Medical Symptom Classifier Service

Wraps a zero-shot HuggingFace classification model to predict possible medical conditions
from patient symptom descriptions. All predictions are saved to PostgreSQL for tracking.

> This service is for **demo/educational purposes only** and is NOT a substitute for professional medical advice.
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware (outermost first) ─────────────────────────────────────────────
app.add_middleware(RequestLoggingMiddleware)

# ── Prometheus /metrics endpoint ─────────────────────────────────────────────
# multiprocess_mode is not needed for a single-worker setup.
# _refresh_system_metrics is called as an additional instrumentation callback
# so CPU/RAM are always fresh when Prometheus scrapes /metrics.
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/metrics"],
).instrument(app, additional_instrumentation_callbacks=[_refresh_system_metrics]).expose(
    app, include_in_schema=False, tags=["Ops"]
)

app.include_router(api.router)
