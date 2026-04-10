"""
Request/response logging middleware.

For every HTTP request this middleware:
  - Clears the structlog context so no state bleeds between requests.
  - Emits a single structured log line on response with: method, path,
    status_code, and duration_ms.
  - Adds an X-Trace-ID response header carrying the active OTel trace ID so
    callers can correlate logs with distributed traces in Grafana Tempo.

trace_id and span_id are injected into every log line automatically by the
_inject_otel_context structlog processor (app/core/logging_config.py); no
manual binding is needed here.
"""
import time

import structlog
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        structlog.contextvars.clear_contextvars()

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.error(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            response.headers["X-Trace-ID"] = format(ctx.trace_id, "032x")

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
