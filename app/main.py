import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import make_asgi_app as make_metrics_app

from app.core.logging_config import configure_logging
from app.core.telemetry import setup_telemetry
from app.middleware.logging import RequestLoggingMiddleware
from app.routers import api
from app.services import core


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    setup_telemetry()
    FastAPIInstrumentor.instrument_app(app)
    await core.init_db()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, core.get_classifier)
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
# PrometheusMetricReader (wired in telemetry.py) bridges OTel metrics into the
# prometheus_client default registry.  make_asgi_app() exposes that registry at
# /metrics on the same port Prometheus already scrapes (api:8000).
app.mount("/metrics", make_metrics_app())

app.include_router(api.router)
