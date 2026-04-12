# AVOIDANCE_TABLE.md — Critical Issues Resolved

Real-world issues encountered and resolved during development.

---

## Mistake 1 — Hard crash when model fails vs silent mock results

**Problem:** Two related issues:
1. If the HuggingFace model download timed out or failed at runtime, the whole service crashed on startup.
2. Previously, the service silently fell back to mock mode (random predictions) to stay alive, which was dangerous for a medical AI service as it gave fake results without an obvious error.

**Fix — Layer 1, pre-download at build time (primary):** Model weights (`facebook/bart-large-mnli`) are downloaded during `docker build` via an `ARG`/`ENV HF_HOME` pattern and stored at `/hf-cache` inside the image. The runtime stage copies `/hf-cache` with read-only permissions for `appuser`, so the model loads from disk with zero network calls.

```dockerfile
# builder stage
ARG MODEL_NAME=facebook/bart-large-mnli
ENV HF_HOME=/hf-cache
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    python -c "from transformers import pipeline; pipeline('zero-shot-classification', model='${MODEL_NAME}')"

# runtime stage
COPY --from=builder /hf-cache /hf-cache
RUN chmod -R a+rX /hf-cache
ENV HF_HOME=/hf-cache
```

**Fix — Layer 2, explicit non-mock behavior (safety net):** We removed silent mock predictions and introduced explicit fallback behavior. The current flow keeps the service alive, attempts the provider chain, and returns `is_fallback=true` with a safe `"unclassifiable"` output when all providers fail.

```python
result_data, is_fallback = await classify_with_fallback(symptoms)
if is_fallback:
    return {
        "top_condition": "unclassifiable",
        "confidence": 0.0,
        "all_predictions": [],
        "is_fallback": True,
    }
```

> **Note:** The Layer 2 behaviour (503 on model failure) was subsequently improved in **Mistake 6**. `classify_symptoms` no longer raises; it returns a safe fallback dict with `is_fallback=True`. `POST /predict` now always returns `201 Created` — see Mistake 6 for the current verification steps.

---

## Mistake 2 — Docker Compose `.env` not loaded when using `-f`

**Problem:** Running `docker compose -f docker/docker-compose.yml up` from the project root caused `POSTGRES_PASSWORD` to resolve to an empty string even though `.env` existed in the project root. Docker Compose v2 sets the *project directory* to the folder containing the first `-f` file (`docker/`), so it looked for `.env` in `docker/` — not the root.

**Symptom:** Warning `The "POSTGRES_PASSWORD" variable is not set. Defaulting to a blank string.` followed by `container medical_db is unhealthy`.

**Fix (compose):** Added `env_file: - ../.env` to both the `api` and `db` services. Variables are injected directly into containers at runtime rather than being interpolated into the compose YAML at parse time, so the `.env` discovery path no longer matters.

**Fix (app):** Removed `DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@db:5432/...` from the `api` environment block. `app/services/core.py` now builds `DATABASE_URL` at runtime from the individual `POSTGRES_*` parts (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`) when `DATABASE_URL` is not explicitly provided, eliminating compose-time secret interpolation entirely.

**Principle:** Avoid `${SECRET}` compose interpolation for sensitive values. Prefer `env_file` so secrets are injected at container runtime, not evaluated at compose parse time.

---

## Mistake 3 — Blocking the event loop with CPU-bound ML tasks

**Problem:** Calling the HuggingFace `pipeline` object synchronously inside an `async def` function blocks the entire FastAPI event loop. Under load, this causes the API to hang and prevents it from handling other requests (like `/health`) while inference is running.

**Fix:** Offloaded the `clf(...)` call to a thread pool using `asyncio.get_running_loop().run_in_executor(None, ...)`. This allows the event loop to continue processing other requests while the CPU-heavy classification runs in the background.

---

## Mistake 4 — Silent data loss (dropping request fields)

**Problem:** The API accepted `age` and `notes` in the request schema, but the service layer didn't store them in PostgreSQL because the columns were missing from the DDL and the INSERT query.

**Fix:** Updated the database schema to include `age` and `notes` columns and modified the persistence logic to include these fields in the `INSERT` statement. Added an `ALTER TABLE` migration guard to ensure existing databases are updated without manual intervention.

---

## Mistake 5 — Resource leaks on shutdown

**Problem:** The `asyncpg` connection pool was initialized on startup but never explicitly closed when the service stopped, leading to hanging connections in PostgreSQL.

**Fix:** Added an explicit `await core.close_db_pool()` call to the FastAPI `lifespan` shutdown phase (after the `yield` statement) to gracefully drain and close all database connections.

---

## Mistake 6 — No retry or fallback for model inference failures

**Problem:** Earlier versions used a single-provider inference path, so transient model errors could fail requests without trying alternative providers.

**Fix — Layer 1, retry with tenacity:** Retries now live at the provider level in `app/services/providers.py`. HuggingFace retries up to 3 times (exponential backoff), while OpenAI/Gemini retry up to 2 times each.

```python
@_hf_breaker
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def classify(self, symptoms: str) -> dict:
    ...
```

**Fix — Layer 2, provider-chain fallback response:** `classify_symptoms` now delegates to `classify_with_fallback()` (HuggingFace → OpenAI → Gemini). If every available provider fails (or circuit breakers are open), it returns a safe default with `is_fallback=True`, `top_condition="unclassifiable"`, `confidence=0.0`, and a Vietnamese advisory message. The router still returns `201` for this model-failure mode.

**Fix — Layer 3, audit persistence:** Fallback records are stored in PostgreSQL with an `is_fallback BOOLEAN` column so the care team can identify unclassified requests and data analysts can exclude them from model metrics.

**How to verify:** Force all providers to fail (e.g., invalid external keys and HuggingFace failure). `POST /predict` should still return `201` with `"is_fallback": true` and `"fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"` instead of `503`.

---

## Mistake 7 — OTel OTLP endpoint path bypassed by explicit `endpoint=` argument

**Problem:** `app/core/telemetry.py` read `OTEL_EXPORTER_OTLP_ENDPOINT` manually via `os.getenv` and passed the raw value as `endpoint=` to `OTLPSpanExporter`. Per the OTel spec, this env var is a *base URL*; the SDK auto-appends the signal-specific path (`/v1/traces`) only when it reads the var itself. Passing an explicit `endpoint=` argument bypasses that logic entirely — the value is used verbatim as the full URL. The built-in default worked only because it already contained `/v1/traces`; any user setting the standard base URL (e.g. `http://collector:4318`) would silently send traces to `http://collector:4318` instead of `http://collector:4318/v1/traces`, losing all trace data with no error.

**Fix:** Removed the manual `os.getenv` call and the `endpoint=` argument. Instead, `os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")` establishes the default base URL before the exporter is constructed, and `OTLPSpanExporter()` is called with no arguments so the SDK reads the env var natively and appends the correct path.

```python
# before — bypasses SDK path-appending
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318/v1/traces")
BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))

# after — SDK reads env var and appends /v1/traces per spec
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")
BatchSpanProcessor(OTLPSpanExporter())
```

Updated `.env.example`, `.env`, and `README.md` to document `OTEL_EXPORTER_OTLP_ENDPOINT` as a base URL.

---

## Mistake 8 — Narrow exception catch lets DB connection failures escape as 500

**Problem:** The `except asyncpg.PostgresError` handler in `app/routers/api.py` only covered server-acknowledged PostgreSQL errors (constraint violations, syntax errors, etc.). When the database is actually *unreachable*, asyncpg raises `asyncpg.InterfaceError` (pool/connection-management errors) or a low-level `OSError` / `ConnectionRefusedError`. Neither inherits from `PostgresError`, so these connection-level exceptions bypassed the handler entirely, producing an unstructured `500 Internal Server Error` instead of the documented `503 Service Unavailable` with a human-readable message.

**Fix:** Broadened the except clause to cover all three error families:

```python
# before — misses connection-level failures
except asyncpg.PostgresError as exc:

# after — catches server errors, pool/connection errors, and OS-level network errors
except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
```

**How to verify:** Stop the `db` container while the API is running (`docker stop medical_db`) and call `POST /predict`. The response should be `503` with `{"detail": "Database unavailable; the prediction could not be saved. Please retry later."}` and a `db_error_on_predict` log line at `ERROR` level.

---

## Mistake 9 — FastAPIInstrumentor.instrument_app called inside lifespan — no spans created

**Problem:** `FastAPIInstrumentor.instrument_app(app)` (and `setup_telemetry()`) were called inside the FastAPI `lifespan` async context manager. Starlette compiles the middleware stack **before** the lifespan handler runs — it needs the compiled stack to process the `lifespan` ASGI scope itself. Any middleware added during lifespan startup is inserted into `user_middleware` but never enters the already-frozen compiled stack. The `OpenTelemetryMiddleware` that creates per-request spans was never part of the live request pipeline. Every request returned `NonRecordingSpan` (OTel no-op), the `X-Trace-ID` header was never set, and no traces reached Grafana Tempo.

**Fix:** Moved `configure_logging()`, `setup_telemetry()`, and `FastAPIInstrumentor.instrument_app(app)` to **module level** — executed at import time, after `app = FastAPI(...)` but before any ASGI call arrives. The `lifespan` retains only runtime I/O startup work (`init_db`, model warm-up).

```python
# before — too late; middleware stack already compiled when lifespan runs
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    setup_telemetry()
    FastAPIInstrumentor.instrument_app(app)   # no-op — stack already frozen
    ...

# after — module level, before first ASGI call
configure_logging()
setup_telemetry()

app = FastAPI(lifespan=lifespan, ...)
app.add_middleware(RequestLoggingMiddleware)
FastAPIInstrumentor.instrument_app(app)       # included in initial stack compile
```

**How to verify:** `curl -si -X POST http://localhost:8000/predict ... | grep -i x-trace-id` must return a 32-hex-char trace ID. In Grafana → Explore → Tempo, searching by that ID must show the full distributed trace.

**Principle:** Middleware — including OTel instrumentation — must be registered **before** the first ASGI call. Starlette compiles the middleware stack once (to handle the `lifespan` scope itself) and does not recompile mid-flight. Use `lifespan` only for I/O startup (DB pool, model warm-up), never for framework-level wiring.

---

## Mistake 10 — Tempo volume mounted at /tmp/tempo causes write-permission failure

**Problem:** `docker-compose.yml` mounted the `tempo_data` named volume at `/tmp/tempo`, and `tempo.yaml` pointed storage paths at `/tmp/tempo/blocks` and `/tmp/tempo/wal`. `grafana/tempo:2.5.0` runs as a non-root user (`tempo`, UID 10001). Docker initialises a named volume with the ownership of the corresponding directory in the image. `/tmp/tempo` does not exist in the Tempo image, so Docker creates the volume root as `root:root 755` — UID 10001 cannot write to it and Tempo fails to start.

This is **not** a Windows Docker Desktop-specific issue. The same failure occurs on Linux and macOS because the volume filesystem follows standard Linux ownership semantics regardless of the host OS. Running Tempo as root (`user: "0"` in the compose file) would unblock it but is unnecessary — the correct fix is to use the path the image already owns.

**Fix:** Changed the volume mount and storage configuration to `/var/tempo`, which the Tempo 2.5.0 image initialises as `tempo:tempo` (UID/GID 10001). No privilege escalation is needed.

```yaml
# docker-compose.yml — before
- tempo_data:/tmp/tempo

# docker-compose.yml — after
- tempo_data:/var/tempo
```

```yaml
# tempo.yaml — before
storage:
  trace:
    backend: local
    local:
      path: /tmp/tempo/blocks
    wal:
      path: /tmp/tempo/wal

# tempo.yaml — after
storage:
  trace:
    backend: local
    local:
      path: /var/tempo/blocks
    wal:
      path: /var/tempo/wal
```

**Principle:** When using a non-root container image, mount named volumes to paths the image already owns (check the image's Dockerfile or release notes). Never use `/tmp/...` as a persistent volume mount point — it bypasses image-configured ownership and creates root-owned volume roots.

---

## Mistake 11 — `http_requests_total` missing from Prometheus because OTel FastAPI instrumentation doesn't create it

**Problem:** After migrating from `prometheus-fastapi-instrumentator` to OpenTelemetry (4.0.0), querying `http_requests_total` or `rate(http_requests_total[1m])` in Prometheus returned no results — no error, just silence. The CHANGELOG 4.0.0 breaking changes noted that "HTTP metric names changed to OTel semantic convention names", but the PromQL examples in the RUNBOOK still referenced `http_requests_total`, making the omission non-obvious.

The root cause: `opentelemetry-instrumentation-fastapi` auto-instruments the app with a histogram named `http.server.request.duration` (OTel semantic conventions), which Prometheus exposes as `http_server_request_duration_seconds_*`. It does **not** create any metric named `http_requests_total`. Prometheus silently returns no data for a metric that doesn't exist — it does not warn that the metric name is unknown.

An additional bug was present in the RUNBOOK PromQL example for error rate:
```promql
# wrong — label name is status_code, not status
rate(http_requests_total{status=~"[45].."}[1m])
```

**Fix:** Added an explicit `prometheus_client.Counter` in `RequestLoggingMiddleware`, which already intercepts every HTTP request:

```python
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests received",
    ["method", "path", "status_code"],
)

# inside dispatch, after response is received:
HTTP_REQUESTS_TOTAL.labels(
    method=request.method,
    path=request.url.path,
    status_code=response.status_code,
).inc()
```

**How to verify:** After rebuilding and making at least one request (`curl http://localhost:8000/health`), open Prometheus at `http://localhost:9090` and query `http_requests_total` — results appear immediately. `rate(http_requests_total[1m])` works for rate-of-requests. Use `{status_code=~"4..|5.."}` (not `{status=~"..."}`) to filter by error codes.

**Principle:** When switching metric libraries, audit every PromQL query in runbooks, dashboards, and alerting rules. Prometheus does not warn on unknown metric names — a typo or renamed metric silently returns an empty result set that looks identical to "no traffic yet".

---

## Mistake 12 — `AsyncPGInstrumentor` crashes tests when asyncpg is mocked as a plain MagicMock

**Problem:** `conftest.py` stubs `asyncpg` with `sys.modules["asyncpg"] = MagicMock()` so the test suite runs without the real asyncpg package. This works fine as long as `opentelemetry-instrumentation-asyncpg` is *not* installed in the test environment. Once the package is installed (e.g. via `pip install -r requirements.txt`), `setup_telemetry()` runs at import time (module-level in `main.py`) and calls `AsyncPGInstrumentor().instrument()`. Internally that function uses `wrapt.wrap_function_wrapper("asyncpg.connection", "Connection.execute", ...)` to monkey-patch the real asyncpg module. `wrapt` calls `__import__("asyncpg.connection")`, which fails because the stub in `sys.modules` is a flat `MagicMock` object — not a package with sub-modules:

```
ModuleNotFoundError: No module named 'asyncpg.connection'; 'asyncpg' is not a package
```

Every test that uses the `client` fixture then errored at setup, not with a helpful message but with a deep traceback through `wrapt`'s internals.

**Why it was not caught earlier:** The original test suite was written when `opentelemetry-instrumentation-asyncpg` was *not* installed in the developer environment. The fixture never triggered the error because the package simply wasn't present.

**Fix:** Add a stub for `opentelemetry.instrumentation.asyncpg` itself *before* `app.main` is imported. This replaces the real instrumentor module with a no-op `MagicMock` so `AsyncPGInstrumentor().instrument()` becomes a harmless mock call:

```python
# tests/conftest.py — add after the asyncpg/transformers/torch stubs
sys.modules["opentelemetry.instrumentation.asyncpg"] = MagicMock()
```

Unlike the `asyncpg` stub (which uses the `if _mod not in sys.modules` guard to avoid double-replacing if the real package happened to load first), this override is applied unconditionally — the goal is always to replace the real instrumentor with the no-op, regardless of whether the package is installed.

**How to verify:** Install `opentelemetry-instrumentation-asyncpg` and run `python -m pytest --no-cov`. All 27 tests should pass without any `ModuleNotFoundError` or `wrapt` traceback.

**Principle:** When a test suite stubs a module with a flat `MagicMock`, also stub any *other* installed package that introspects or monkey-patches that module at import time. Instrumentation libraries (OTel, `wrapt`, `ddtrace`, etc.) that patch the module's internal sub-modules will fail when the stub is a plain object rather than a real package hierarchy.

---

## Mistake 13 — Using `pybreaker` for async code

**Problem:** The original implementation plan called for `pybreaker` to add circuit-breaker protection around provider calls. `pybreaker.CircuitBreaker` wraps synchronous callables. When used as a decorator or call wrapper on `async def` functions it does not await the coroutine — it calls the coroutine factory without `await`, so the coroutine is created but never executed and errors are never counted. The breaker opens only on synchronous exceptions; async failures pass through silently.

**Fix:** Use `aiobreaker` instead, which is the async-native circuit breaker library. It wraps `async def` functions correctly with `await` and tracks async exceptions:

```python
from aiobreaker import CircuitBreaker, CircuitBreakerError

_hf_breaker = CircuitBreaker(fail_max=3, timeout_duration=60)

@_hf_breaker
async def classify(self, symptoms: str) -> dict:
    ...
```

Catch `CircuitBreakerError` separately in the fallback chain to distinguish "breaker is open" from "provider actually failed this request":

```python
try:
    result = await provider.classify(symptoms)
except CircuitBreakerError:
    logger.warning("provider_circuit_open", provider=provider.name)
except Exception as exc:
    logger.warning("provider_failed", provider=provider.name, error=str(exc))
```

**Principle:** Never assume a sync circuit-breaker library works with async functions. Always check whether the library uses `await` internally. The name may look compatible but the actual call mechanics are completely different.

---

## Mistake 14 — AlertManager YAML does not expand `${ENV_VAR}` syntax

**Problem:** AlertManager's YAML parser reads the config file as a plain YAML document. It does not perform shell-style environment variable substitution. Writing `api_url: "${SLACK_WEBHOOK_URL}"` results in the literal string `"${SLACK_WEBHOOK_URL}"` being sent as the webhook URL — AlertManager will attempt to POST to that address and silently receive connection errors.

**Fix:** Write the Slack webhook URL as a literal value directly in `alertmanager.yml`. To prevent the secret from being committed:

1. Create `alertmanager.yml.example` with a placeholder URL — commit this file.
2. Add `docker/alertmanager.yml` to `.gitignore`.
3. Document in the RUNBOOK that operators must copy the example and fill in the real URL before starting the stack.

If automation is needed, use `envsubst` in a Makefile target or an entrypoint script to produce the file from the template at deploy time:

```bash
envsubst < docker/alertmanager.yml.example > docker/alertmanager.yml
```

**Principle:** AlertManager, Prometheus, and most YAML-based config systems do not perform runtime env-var expansion. Only Docker Compose YAML and shell scripts do this natively. Always check the tool's documentation before relying on `${VAR}` syntax.

---

## Mistake 15 — Grafana dashboard JSON is silently ignored without a provider config

**Problem:** Creating a dashboard JSON file under `docker/grafana/provisioning/dashboards/` is not sufficient. Grafana reads provisioning configuration from `provisioning/dashboards/*.yaml` (the *provider* config) to know which directory to scan and how to interpret the files. Without a `dashboards.yaml` provider file, Grafana starts normally but never discovers the dashboard JSON — no error is shown, the dashboard simply does not appear.

**Fix:** Create `docker/grafana/provisioning/dashboards/dashboards.yaml` alongside the JSON file:

```yaml
apiVersion: 1
providers:
  - name: medical-ai
    folder: Medical AI
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards
```

This file tells Grafana to treat the directory as a file-based dashboard source. The Grafana container's `provisioning/` volume must already be mounted (verify in `docker-compose.yml`).

**Principle:** When adding provisioning resources to Grafana (dashboards, datasources, alerting), always check whether a *provider* YAML is needed in addition to the resource file. Datasources use `datasources/*.yaml`; dashboards require a separate `dashboards/*.yaml` provider entry.

---

## Mistake 16 — `CORSMiddleware` with an unset `ALLOWED_ORIGINS` env var produces `[""]`

**Problem:** The pattern `os.getenv("ALLOWED_ORIGINS", "").split(",")` returns `[""]` when `ALLOWED_ORIGINS` is not set (or set to an empty string). A single-element list containing the empty string is passed to `CORSMiddleware` as `allow_origins`. The empty string is not a valid origin; Starlette either rejects all CORS pre-flight requests or behaves unpredictably depending on the version.

**Fix:** Filter empty strings after splitting:

```python
allow_origins=[
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
```

This produces an empty list `[]` when the variable is unset — which means no origins are allowed (correct default for an API service) — and a properly filtered list when origins are provided.

**Principle:** Any time you split an env var by a delimiter and use the result as a list, filter out empty strings. `"".split(",")` is `[""]`, not `[]`.
