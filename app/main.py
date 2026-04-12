import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import make_asgi_app as make_metrics_app
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app.core.limiter import limiter
from app.core.logging_config import configure_logging
from app.core.telemetry import setup_telemetry
from app.middleware.logging import RequestLoggingMiddleware
from app.routers import api, auth
from app.services import core
from app.services.providers import _hf_provider

# Must run at module level — before the ASGI server builds the middleware stack.
configure_logging()
setup_telemetry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.init_db()
    loop = asyncio.get_running_loop()
    # Warm up the HuggingFaceProvider singleton so the first real request
    # doesn't pay the cold-start penalty.
    await loop.run_in_executor(None, _hf_provider._load)
    yield
    await core.close_db_pool()


app = FastAPI(
    lifespan=lifespan,
    title="Medical Symptom Classifier API",
    description="""
## Medical Symptom Classifier Service

Wraps a zero-shot HuggingFace classification model (with OpenAI and Gemini
fallbacks) to predict possible medical conditions from patient symptom
descriptions.  All predictions are saved to PostgreSQL for tracking.

### Authentication
Most endpoints require authentication. Use either `X-API-Key` or a Bearer token
from `POST /auth/token`.

> This service is for **demo/educational purposes only** and is NOT a substitute for professional medical advice.
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Rate limiter state ────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────────────────────
# Filter empty strings so ALLOWED_ORIGINS="" doesn't produce [""].
_allowed_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Authorization", "Content-Type"],
)

# ── Security headers ──────────────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add defensive HTTP headers to every response.

    Strict-Transport-Security is only enforced by browsers over HTTPS;
    it is harmless but inert when the service is running behind plain HTTP.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ── Request logging ───────────────────────────────────────────────────────────
app.add_middleware(RequestLoggingMiddleware)

# ── OTel FastAPI instrumentation ──────────────────────────────────────────────
FastAPIInstrumentor.instrument_app(app)

# ── Prometheus /metrics endpoint ──────────────────────────────────────────────
app.mount("/metrics", make_metrics_app())

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(api.router)
