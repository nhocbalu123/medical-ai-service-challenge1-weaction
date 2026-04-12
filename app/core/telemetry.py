"""
Central OpenTelemetry wiring.

Call setup_telemetry() once at module import time, before the FastAPI app is
created, so middleware and instrumentation are registered early enough to
observe startup and requests. Do not move this call into the lifespan context.

Trace pipeline:  FastAPI → OTel SDK → OTLP/HTTP → Grafana Tempo
Metric pipeline: OTel SDK → PrometheusMetricReader → prometheus_client
                 registry → GET /metrics (mounted via make_asgi_app)
"""
import os

import psutil
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: F401
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry() -> None:
    """
    Wire OTel trace + metric providers and auto-instrument asyncpg.

    The OTLP endpoint and service name are read from environment variables so
    the same image works in every environment without rebuilding:

        OTEL_SERVICE_NAME              (default: medical-ai-service)
        OTEL_EXPORTER_OTLP_ENDPOINT   (default: http://tempo:4318)
                                       Must be a base URL — the SDK auto-appends
                                       the signal-specific path (/v1/traces).
        OTEL_RESOURCE_ATTRIBUTES       (optional, e.g. deployment.environment=dev)
    """
    service_name = os.getenv("OTEL_SERVICE_NAME", "medical-ai-service")

    # Set the default base URL *before* the exporter is constructed so the SDK
    # reads it natively and appends /v1/traces per the OTel spec.  Never pass
    # the value as endpoint= — that bypasses the SDK's path-appending logic.
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")

    resource = Resource.create({"service.name": service_name})

    # ── Traces → Tempo via OTLP/HTTP ────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics → Prometheus scrape endpoint ────────────────────────────────
    # PrometheusMetricReader bridges the OTel MeterProvider to the
    # prometheus_client default registry.  The actual /metrics HTTP route is
    # mounted in main.py via prometheus_client.make_asgi_app().
    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # ── psutil CPU / RAM gauges ──────────────────────────────────────────────
    meter = metrics.get_meter(service_name)
    _proc = psutil.Process()

    def _cpu_callback(opts):  # noqa: ARG001
        return [metrics.Observation(_proc.cpu_percent(interval=None))]

    def _ram_callback(opts):  # noqa: ARG001
        return [metrics.Observation(_proc.memory_info().rss)]

    meter.create_observable_gauge(
        "process_cpu_percent",
        callbacks=[_cpu_callback],
        description="Current process CPU usage percent",
    )
    meter.create_observable_gauge(
        "process_rss_bytes",
        callbacks=[_ram_callback],
        description="Current process RSS memory in bytes",
    )

    # ── asyncpg auto-instrumentation ────────────────────────────────────────
    AsyncPGInstrumentor().instrument()
