# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Multi-provider LLM fallback chain (`app/services/providers.py`).** Classification now tries providers in order: HuggingFace (local, retry 3×) → OpenAI (retry 2×) → Gemini (retry 2×) → hardcoded `"unclassifiable"` fallback. Each provider has an `aiobreaker` circuit breaker (fail\_max=3, 60 s reset) to skip failing providers immediately after repeated errors. Providers without credentials (`OPENAI_API_KEY`, `GEMINI_API_KEY`) are automatically skipped.

- **`app/core/constants.py`** — single source of truth for `CONDITION_LABELS`, imported by both `core.py` and `providers.py`.

- **Custom OTel metrics (`app/services/core.py`).** Three new instruments recorded per classification request:
    - `medical_ai_predictions_total` (counter, label: `provider`) — total predictions by provider.
    - `medical_ai_fallback_total` (counter) — total hardcoded-fallback responses.
    - `medical_ai_inference_duration_ms` (histogram, label: `provider`) — end-to-end inference latency.

- **Prometheus AlertManager (`docker/prometheus/alerts.yml`, `docker/alertmanager.yml`).** Five alert rules: `HighErrorRate` (5xx > 5%), `SlowResponses` (P95 > 10 s), `HighFallbackRate` (fallback > 20%), `HighMemoryUsage` (RSS > 3.4 GB), `HighCPUUsage` (CPU > 90%). AlertManager routes to Slack with critical/warning separation and inhibition rules. `docker/prometheus.yml` updated with `rule_files` and `alerting` sections. Prometheus started with `--web.enable-lifecycle` for config reload without restart.

- **Grafana dashboard provisioning (`docker/grafana/provisioning/dashboards/`).** `dashboards.yaml` provider config (required for Grafana to discover JSON files) and `medical-ai.json` dashboard with 8 panels: request rate, 5xx error rate, fallback rate, RAM, P50/P95/P99 latency, inference latency by provider, prediction rate by provider, CPU %.

- **Protected-route authentication (`app/core/security.py`).** `require_api_key` FastAPI dependency uses `hmac.compare_digest` over SHA-256 hashes for timing-safe API-key comparison and also accepts valid Bearer JWTs. Supports multiple keys via `API_KEYS=key1,key2`. Dev-mode bypass when `API_KEYS` is unset. Applied to `POST /predict` and `GET /predict/{id}`; `/health` and `/metrics` remain public.

- **JWT authentication (`app/core/auth.py`, `app/routers/auth.py`).** `POST /auth/token` issues HS256 Bearer tokens. `get_current_user` dependency validates tokens. User store is in-memory (seeded from `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars) — replace with DB query for production.

- **Rate limiting (`app/core/limiter.py`).** `slowapi` limiter at 30 requests/minute per IP on `POST /predict`. Limiter state lives in `app.state.limiter`; shared singleton in `app/core/limiter.py` avoids circular imports.

- **Security headers middleware (`app/main.py`).** `SecurityHeadersMiddleware` adds `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Strict-Transport-Security` (effective only over HTTPS/TLS proxy) to every response.

- **CORS middleware (`app/main.py`).** `CORSMiddleware` configured from `ALLOWED_ORIGINS` env var. Empty values are filtered so `ALLOWED_ORIGINS=""` does not produce `[""]`.

- **New dependencies:** `openai>=1.30.0`, `aiobreaker>=1.0`, `python-jose[cryptography]>=3.3.0`, `passlib[bcrypt]>=1.7.4`, `slowapi>=0.1.9`.

- **New environment variables:** `OPENAI_API_KEY`, `OPENAI_MODEL`, `GEMINI_API_KEY`, `API_KEYS`, `JWT_SECRET_KEY`, `JWT_EXPIRE_MINUTES`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ALLOWED_ORIGINS`. All optional (documented in `.env.example`).

- **`docker/alertmanager.yml.example`** — template with placeholder Slack webhook URL. The real `docker/alertmanager.yml` is listed in `.gitignore` to prevent committing secrets.

### Changed

- **`app/services/core.py`** — inference path replaced with `classify_with_fallback()`; Langfuse trace now wraps the entire fallback chain (one trace per request regardless of provider used); `provider` column added to `predictions` table (migration guard with `ADD COLUMN IF NOT EXISTS`); `MODEL_VERSION` persisted per-row.

- **`app/main.py`** — lifespan warm-up changed from `core.get_classifier()` to `providers._hf_provider._load()` to warm up the correct singleton; auth and API routers both registered.

- **`app/routers/api.py` / `app/services/core.py`** — `/health` now returns `model` from module-level `MODEL_NAME` (mirrors `MODEL_VERSION`) instead of a hardcoded string.

- **`docker/docker-compose.yml`** — `api` service receives all new env vars; `prometheus` service mounts `alerts.yml` and runs with `--web.enable-lifecycle`; `alertmanager` service added.

- **`.gitignore`** — `docker/alertmanager.yml` added.

---

- **12 new test cases** in `tests/test_api.py` filling the gaps identified in the coverage audit:
    - *High severity:* `POST /predict` → 503 on `asyncpg.PostgresError` and `OSError`; `GET /predict/abc` → 422 on non-integer path; `GET /predict/{id}` returns `fallback_message` when the stored record has `is_fallback=True`.
    - *Medium severity:* `patient_id` > 64 chars → 422; `notes` > 500 chars → 422; `symptoms` > 1000 chars → 422; explicit assertion that `GET /health` returns `model=unavailable` when `get_classifier` returns `None`.
    - *Low severity:* `GET /metrics` reachable and contains `http_requests_total`; `check_db_health` unit tests for success and silent-failure paths; `_run_inference` retry-count assertion (tenacity calls classifier exactly 3 times before re-raising).
- **`pytest.ini`** at repo root — configures `testpaths = tests`, enables `--cov=app --cov-report=term-missing`, and enforces a 70 % coverage floor (`--cov-fail-under=70`). Running `python -m pytest` now produces a full coverage report automatically.
- **`pytest-cov==7.1.0`** added to `requirements-dev.txt`.

### Fixed

- **`tests/conftest.py`**: Added `sys.modules["opentelemetry.instrumentation.asyncpg"] = MagicMock()` stub. When `opentelemetry-instrumentation-asyncpg` is installed in the environment, its `AsyncPGInstrumentor().instrument()` call (executed at module-level in `main.py` via `setup_telemetry()`) used `wrapt` to patch `asyncpg.connection.Connection.execute`. Because `asyncpg` is stubbed as a plain `MagicMock` in the test environment, `wrapt` raised `ModuleNotFoundError: No module named 'asyncpg.connection'; 'asyncpg' is not a package`. The new stub replaces the real instrumentor with a no-op MagicMock so the import chain completes cleanly.
- **`docs/RUNBOOK.md`**: Added section 2 "Running the Test Suite" documenting `pip install -r requirements-dev.txt`, `python -m pytest`, per-test invocation, and what each area of the suite covers. Existing sections renumbered 3–7.

---

## [4.0.1] - 2026-04-11

### Fixed

- **`http_requests_total` missing from Prometheus — OTel FastAPI instrumentation does not produce this metric.** Querying `http_requests_total` or `rate(http_requests_total[1m])` in Prometheus returned no results because `opentelemetry-instrumentation-fastapi` generates histogram metrics under OTel semantic convention names (e.g. `http_server_request_duration_seconds_*`), not a counter named `http_requests_total`. The metric simply did not exist in the registry.
    - `app/middleware/logging.py`: Added an explicit `prometheus_client.Counter` named `http_requests_total` with labels `method`, `path`, and `status_code`. The counter is incremented on every HTTP response inside `RequestLoggingMiddleware.dispatch`, which already intercepts every request.
    - `docs/RUNBOOK.md`: Fixed the error-rate PromQL query — label name corrected from `status` to `status_code` to match the counter definition.
    - `docs/AVOIDANCE_TABLE.md`: Mistake 11 added.
    - Note: this partially reverts the 4.0.0 breaking change that removed `http_requests_total`; dashboards and alerting rules that target that metric name are valid again.

- **`POST /predict` DB error handler broadened to cover connection failures.** The original `except asyncpg.PostgresError` clause only caught server-acknowledged errors (constraint violations, syntax errors, etc.). When the database is actually _unreachable_, asyncpg raises `asyncpg.InterfaceError` (pool/connection errors) or a low-level `OSError` / `ConnectionRefusedError` — neither of which inherits from `PostgresError`. These escaped the handler entirely, producing an unstructured `500` instead of the documented `503`.
    - `app/routers/api.py`: except clause broadened to `(asyncpg.PostgresError, asyncpg.InterfaceError, OSError)`.
    - `app/services/core.py`: docstring updated to accurately document that model errors are suppressed while database errors propagate to the caller.

- **OTel OTLP trace endpoint constructed incorrectly.** `app/core/telemetry.py` read `OTEL_EXPORTER_OTLP_ENDPOINT` manually via `os.getenv` and passed the raw value as `endpoint=` to `OTLPSpanExporter`. Per the OTel spec the env var is a _base URL_; the SDK auto-appends `/v1/traces` only when it reads the var itself. Passing an explicit `endpoint=` argument bypasses that logic, so any user setting the standard base URL (e.g. `http://collector:4318`) would silently send traces to the wrong path.
    - `app/core/telemetry.py`: replaced `os.getenv(...)` + `OTLPSpanExporter(endpoint=...)` with `os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")` and `OTLPSpanExporter()` (no arguments) so the SDK owns path construction.
    - `.env.example`, `.env`, `README.md`: `OTEL_EXPORTER_OTLP_ENDPOINT` example value updated from `http://tempo:4318/v1/traces` to the correct base URL `http://tempo:4318`.

- **`docker/docker-compose.yml` Compose-level default for `OTEL_EXPORTER_OTLP_ENDPOINT` still contained `/v1/traces`.** The previous fix updated `telemetry.py`, `.env.example`, and `README.md` but left the Compose inline default as `http://tempo:4318/v1/traces`. Because Docker Compose injects this value before `os.environ.setdefault` in `telemetry.py` can run, the env var was already set to the full signal URL. The SDK then appended `/v1/traces` a second time, producing `http://tempo:4318/v1/traces/v1/traces` — all traces silently 404'd.
    - `docker/docker-compose.yml`: Compose default changed from `http://tempo:4318/v1/traces` to `http://tempo:4318`.

- **`X-Trace-ID` header absent from all responses; no traces reaching Grafana Tempo.** `configure_logging()`, `setup_telemetry()`, and `FastAPIInstrumentor.instrument_app(app)` were called inside the FastAPI `lifespan` context manager. Starlette compiles the middleware stack before the lifespan handler runs (it needs the compiled stack to process the `lifespan` ASGI scope itself), so the `OpenTelemetryMiddleware` was inserted into `user_middleware` after the stack was already frozen — it never participated in the live request pipeline. Every request returned a `NonRecordingSpan` (OTel no-op); `X-Trace-ID` was never set.
    - `app/main.py`: `configure_logging()` and `setup_telemetry()` moved to module level (before `app = FastAPI(...)`); `FastAPIInstrumentor.instrument_app(app)` moved to module level (after `app.add_middleware(...)`, before `app.mount()`). `lifespan` now contains only runtime I/O startup (`init_db`, model warm-up).
    - `docs/AVOIDANCE_TABLE.md`: Mistake 9 added (previous Mistake 9 renumbered to 10).
    - `docs/RUNBOOK.md`: `X-Trace-ID` troubleshooting row updated with accurate cause and fix.

- **Tempo named volume mounted at wrong path caused write-permission failure on startup.** `docker-compose.yml` mounted `tempo_data` at `/tmp/tempo` and `tempo.yaml` pointed storage paths at `/tmp/tempo/blocks` and `/tmp/tempo/wal`. `grafana/tempo:2.5.0` runs as a non-root user (`tempo`, UID 10001). Docker initialises a named volume with the ownership of the corresponding directory inside the image; `/tmp/tempo` does not exist in the Tempo image, so the volume root was created as `root:root 755`. UID 10001 had no write access and Tempo failed to start.
    - `docker/docker-compose.yml`: volume mount changed from `tempo_data:/tmp/tempo` to `tempo_data:/var/tempo` (the path the image pre-owns as `tempo:tempo`).
    - `docker/tempo.yaml`: storage paths updated from `/tmp/tempo/blocks` → `/var/tempo/blocks` and `/tmp/tempo/wal` → `/var/tempo/wal`.
    - `docs/AVOIDANCE_TABLE.md`: Mistake 9 added documenting root cause and the principle of always mounting volumes to paths the image already owns.
    - `docs/RUNBOOK.md`: troubleshooting row added for `medical_tempo exits immediately on start`.

---

## [4.0.0] - 2026-04-10

### Added

- **OpenTelemetry SDK** (`opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-asyncpg`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-exporter-prometheus`) replacing `prometheus-fastapi-instrumentator`.
- **`app/core/telemetry.py`** — central OTel wiring: trace provider (OTLP/HTTP → Grafana Tempo), metric provider (`PrometheusMetricReader` → `/metrics`), asyncpg auto-instrumentation via `AsyncPGInstrumentor`, and `process_cpu_percent` / `process_rss_bytes` observable gauges registered on the OTel meter.
- **`_inject_otel_context` structlog processor** (`app/core/logging_config.py`) — stamps every log line with `trace_id` and `span_id` sourced from the active OTel span so logs and traces can be correlated without manual binding.
- **Grafana Tempo** service (`grafana/tempo:2.5.0`) in `docker/docker-compose.yml` with OTLP/HTTP receiver on port `4318` and query API on port `3200`. `docker/tempo.yaml` contains the minimal Tempo configuration.
- **Tempo datasource** pre-provisioned in Grafana via `docker/grafana/provisioning/datasources/tempo.yml`. Distributed traces are immediately visible in Grafana Explore → Tempo.
- New env vars: `OTEL_SERVICE_NAME` (default `medical-ai-service`), `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://tempo:4318/v1/traces`), `OTEL_RESOURCE_ATTRIBUTES` (default `deployment.environment=dev`). All optional; documented in `.env.example`.

### Changed

- **`app/main.py`**: `prometheus_client.Gauge` + `prometheus-fastapi-instrumentator` replaced by OTel observable gauges + `FastAPIInstrumentor`; `/metrics` endpoint now served via `prometheus_client.make_asgi_app()` mounted on the existing FastAPI app (preserves the same `api:8000/metrics` scrape target used by Prometheus).
- **`app/middleware/logging.py`**: UUID `request_id` generation removed; `trace_id` and `span_id` are now injected into logs by the structlog processor. `X-Trace-ID` response header replaces `X-Request-ID`, carrying the W3C-compatible hex trace ID.
- **`docker/docker-compose.yml`**: `api` service now depends on `tempo` (service*started) and receives `OTEL*\*`env vars;`tempo_data` named volume added.

### Removed

- `prometheus-fastapi-instrumentator==7.1.0` dependency.

### Breaking Changes

- **`X-Request-ID` response header removed** — replaced by `X-Trace-ID`. Callers that read `X-Request-ID` must update to `X-Trace-ID`.
- **HTTP metric names changed** from instrumentator-style (`http_requests_total`, `http_request_duration_seconds`) to OTel semantic convention names. Existing Grafana dashboards or alerting rules referencing the old metric names must be updated.

---

## [3.0.0] - 2026-04-08

### Added

- **Structured logging (`structlog`).** All log output is now structured JSON by default (configurable via `LOG_FORMAT=json|console`). Log lines include `timestamp`, `level`, `logger`, and typed key-value fields instead of free-text strings. All existing `logging.getLogger` calls in `core.py` replaced with `structlog.get_logger` using key-value style (e.g. `model=MODEL_NAME` instead of f-string messages).
- **Request/response logging middleware (`app/middleware/logging.py`).** A `BaseHTTPMiddleware` now runs for every request. It generates a UUID `request_id`, binds it to the structlog context (so all log lines during that request carry it), adds an `X-Request-ID` response header, and emits a `request_completed` log line with `method`, `path`, `status_code`, and `duration_ms`.
- **Prometheus metrics (`/metrics` endpoint).** `prometheus-fastapi-instrumentator` auto-instruments all routes, exposing `http_requests_total` and `http_request_duration_seconds` histograms. Two custom `psutil`-based gauges — `process_cpu_percent` and `process_rss_bytes` — are refreshed on every Prometheus scrape.
- **Langfuse LLM tracing (`app/services/core.py`).** Each call to `classify_symptoms` creates a Langfuse trace with a `zero-shot-classification` generation span recording the model name, input symptoms, top condition output, and token count. Tracing is opt-in: it is silently disabled when `LANGFUSE_PUBLIC_KEY` is not set, so the service works without a Langfuse instance.
- **Local observability demo stack (`docker/docker-compose.yml`).** Two new services added under a clearly marked `# Local observability demo` comment: `prometheus` (`prom/prometheus:v2.53.0`, port 9090) and `grafana` (`grafana/grafana:11.0.0`, port 3000). Grafana starts with the Prometheus datasource pre-provisioned via `docker/grafana/provisioning/`.
- **`docker/prometheus.yml`** Prometheus scrape config targeting `api:8000` at `/metrics` every 15 s.
- **`app/core/logging_config.py`** — `configure_logging()` function that wires structlog with a `ProcessorFormatter` over the stdlib logging system; called once at app startup from `main.py`.
- New dependencies: `structlog==25.5.0`, `prometheus-fastapi-instrumentator==7.1.0`, `langfuse==4.0.6`, `psutil==7.2.2`.
- New environment variables: `LOG_FORMAT`, `LOG_LEVEL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `GRAFANA_USER`, `GRAFANA_PASSWORD` (all optional, documented in `.env.example`).

### Changed

- `app/main.py` updated to call `configure_logging()` at startup, register `RequestLoggingMiddleware`, and wire the Prometheus instrumentator.
- Docker Compose `api` service now forwards `LOG_FORMAT`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` environment variables into the container.

---

## [2.2.0] - 2026-04-08

### Added

- **Retry logic for model inference (`tenacity`).** The HuggingFace inference call is now wrapped with `tenacity` retry logic: up to 3 attempts with exponential backoff (1 s → 8 s). Transient inference failures (e.g. GPU OOM, timeout) are retried automatically before triggering the fallback path. A warning is logged before each retry.
- **Default fallback response.** When the classifier is `None` (failed to load) or all inference retries are exhausted, `classify_symptoms` no longer raises `RuntimeError`. Instead it returns a safe default: `top_condition="unclassifiable"`, `confidence=0.0`, `all_predictions=[]`, and a Vietnamese advisory message in `fallback_message` ("Không thể phân loại, vui lòng tham khảo bác sĩ"). The API always returns `201 Created`; callers should inspect the `is_fallback` field.
- **`is_fallback` column in `predictions` table.** Fallback records are persisted to PostgreSQL with `is_fallback=TRUE` so analysts can exclude them from model performance metrics and clinicians have a full audit trail. An `ADD COLUMN IF NOT EXISTS` migration guard handles existing databases automatically.
- **`is_fallback` and `fallback_message` in `PredictionResponse`.** Both fields are now part of the API response schema. `is_fallback` defaults to `false` for normal results; `fallback_message` is `null` unless the fallback path was taken.
- **`tenacity==8.2.3`** added to `requirements.txt`.

### Changed

- **`POST /predict` never returns `503`.** The `try/except RuntimeError` that converted a load failure to HTTP 503 has been removed from `api.py`. Model unavailability is now surfaced gracefully via `is_fallback=true` in the response body.

---

## [2.1.0] - 2026-04-07

### Fixed

- **Awaited model warm-up in lifespan.** The service now waits for the HuggingFace model to finish loading into memory before it starts accepting HTTP requests. This prevents early requests from hitting a `503 Service Unavailable` error during the first few seconds of startup.
- **Offloaded inference to thread pool.** Synchronous HuggingFace `pipeline` calls are now executed in a separate thread pool using `run_in_executor`. This prevents the CPU-heavy classification logic from blocking the FastAPI/asyncio event loop, ensuring the API remains responsive under load.
- **Persisted `age` and `notes` in database.** Added missing `age` and `notes` columns to the `predictions` table and updated the INSERT query to store these fields. Added an `ALTER TABLE` migration guard to `init_db` to automatically update existing databases.
- **Added graceful shutdown for DB pool.** The `asyncpg` connection pool is now explicitly closed during the FastAPI lifespan shutdown phase, preventing hanging connections and ensuring a clean service exit.

### Changed

- **Removed dangerous mock-mode fallback.** If the HuggingFace model fails to load, the service no longer returns fake random predictions. Instead, `POST /predict` now explicitly returns a `503 Service Unavailable` error, and `GET /health` reports `"model": "unavailable"`.

### Fixed

- **`medical_db` container unhealthy on startup.** When Docker Compose is
  invoked with `-f docker/docker-compose.yml` from the project root, Compose v2
  sets the _project directory_ to `docker/` and therefore looks for `.env` there
  — not in the project root. `POSTGRES_PASSWORD` resolved to an empty string,
  PostgreSQL started without a password, and its `pg_isready` health check failed.
- Added `env_file: - ../.env` to both `api` and `db` services in
  `docker/docker-compose.yml`. Variables are now injected directly into each
  container by Docker Compose at runtime rather than being interpolated into the
  compose YAML at parse time, so the `.env` discovery path no longer matters.
- Removed `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}` from the `db` service
  `environment` block — `env_file` passes it directly without compose
  interpolation.
- Replaced `DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@db:5432/...`
  in the `api` service `environment` block with `POSTGRES_HOST: db`.
  `app/services/core.py` now builds `DATABASE_URL` at runtime from the
  individual `POSTGRES_*` parts (`POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`) when `DATABASE_URL` is not
  explicitly provided, eliminating any need for compose-time secret interpolation.

---

## [2.0.0] - 2026-04-06

### Breaking Changes

- **`DATABASE_URL` is now required.** The service raises `RuntimeError` at
  startup if the environment variable is not set. The former silent fallback
  `postgresql://postgres:postgres@db:5432/medicaldb` has been removed.
  Copy `.env.example` to `.env` and supply a real password before running.
- **`POSTGRES_PASSWORD` is now required in `docker-compose.yml`.** The
  `:-postgres` default has been removed from both the `api` and `db` service
  environment blocks. Docker Compose will refuse to start if the variable is
  absent from the environment or a `.env` file.

### Security

- Removed hardcoded `postgres:postgres` credential fallback from
  `app/services/core.py` and `docker/docker-compose.yml` ([#sec-1]).
- Removed the host-side port mapping `5432:5432` for the `db` service in
  `docker/docker-compose.yml`. PostgreSQL is now reachable only from within
  the `medical_net` Docker network; it is no longer accessible from the host
  machine or external networks.

### Fixed

- Replaced deprecated `@router.on_event("startup")` with a `lifespan` async
  context manager on the `FastAPI` instance (`app/main.py`, `app/routers/api.py`).
  `on_event` has been deprecated since FastAPI 0.93 and will be removed in a
  future release.
- Replaced `asyncio.get_event_loop().run_in_executor(...)` with
  `asyncio.get_running_loop().run_in_executor(...)`. `get_event_loop()` emits
  a `DeprecationWarning` in Python 3.10+ and is scheduled for removal in a
  future Python release.

### Changed

- Reduced Uvicorn worker count from `--workers 2` to `--workers 1` in
  `docker/Dockerfile`. Each worker is a separate process that loads its own
  copy of `facebook/bart-large-mnli` (~1.6 GB), so two workers consume ~3.2 GB
  of RAM and risk OOM-killing on standard machines.
- `all_predictions` in `PredictionResponse` is now typed as
  `list[ConditionScore]` (a new `BaseModel` with `label: str` and
  `score: float`) instead of the untyped `list[dict]`. This enables proper
  request validation and generates a richer OpenAPI schema.
- `import json` and `import random` moved from inside functions to the
  top-level import block in `app/services/core.py`.

### Added

- `.env.example` at the project root documenting all required and optional
  environment variables (`POSTGRES_PASSWORD`, `POSTGRES_DB`, `MODEL_NAME`,
  `MODEL_VERSION`).
- `tests/` directory with smoke tests covering:
    - `GET /health` — healthy and degraded-DB states
    - `POST /predict` — happy path (201), optional-field forwarding, four
      validation-error cases (short symptoms, blank symptoms, missing
      `patient_id`, out-of-range age)
    - `GET /predict/{id}` — found and not-found (404) cases
- `tests/conftest.py` bootstrapping `DATABASE_URL` and stubbing native
  dependencies (`asyncpg`, `transformers`, `torch`) so the test suite runs
  without the full ML stack installed.
- `requirements-dev.txt` with `pytest==8.3.4` for running the test suite.
- `__init__.py` files for `app/`, `app/services/`, `app/routers/`,
  `app/models/`, and `tests/` to declare them as proper Python packages.

### Removed

- Unused `from functools import lru_cache` import from `app/services/core.py`.

---

## [1.0.0] - 2026-04-05

### Added

- Initial release.
- `POST /predict` endpoint: zero-shot classification via
  `facebook/bart-large-mnli`, result persisted to PostgreSQL.
- `GET /predict/{id}` endpoint: retrieve a stored prediction by record ID.
- `GET /health` endpoint: liveness check for API, database, and model.
- Pydantic v2 request/response schemas with field validation and 422 error
  responses on bad input.
- asyncpg connection pool with `init_db` table creation on startup.
- Graceful mock-mode fallback when the model fails to load.
- Multi-stage Docker build (`python:3.11-slim`) with model weights
  pre-downloaded at build time.
- Docker Compose stack (`api` + `db`) with healthchecks and restart policies.
- Non-root container user (`appuser`) for least-privilege execution.
- `docs/RUNBOOK.md` and `docs/AVOIDANCE_TABLE.md`.

---

[Unreleased]: https://github.com/nhocbalu123/medical-ai-service/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/nhocbalu123/medical-ai-service/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/nhocbalu123/medical-ai-service/compare/v2.2.0...v3.0.0
[2.2.0]: https://github.com/nhocbalu123/medical-ai-service/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/nhocbalu123/medical-ai-service/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/nhocbalu123/medical-ai-service/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/nhocbalu123/medical-ai-service/releases/tag/v1.0.0
