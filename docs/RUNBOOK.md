# RUNBOOK.md — Operations Guide

## Service: Medical Symptom Classifier API

This repository is a standalone backend service, built as a monolith, organized in a layered architecture.

---

## 📑 Table of Contents

- [1. Running the Service](#1-running-the-service)
- [2. Running the Test Suite](#2-running-the-test-suite)
- [3. Testing the Endpoints (curl)](#3-testing-the-endpoints-curl)
- [4. DB Schema](#4-db-schema)
- [5. Fallback Behaviour](#5-fallback-behaviour)
- [6. Observability](#6-observability)
- [7. Troubleshooting](#7-troubleshooting)
- [8. Authentication Guide](#8-authentication-guide)
- [9. Multi-Provider Fallback](#9-multi-provider-fallback)
- [10. Alerting Setup](#10-alerting-setup)

---

## 1. Running the Service

### Prerequisites

- Docker Desktop (or Docker Engine + Compose plugin)
- 4 GB RAM minimum (for HuggingFace model)
- ~3 GB free disk space (model weights are baked into the image at build time)
- Ports **8000** (API), **9090** (Prometheus), **3000** (Grafana), **4318** (Tempo OTLP ingest), and **3200** (Tempo query API) free on the host. PostgreSQL runs inside the Docker network only — port 5432 is **not** published to the host.
- Internet access during `docker build` (to download `facebook/bart-large-mnli` weights once)

### Start

```bash
# From repo root
# First build is slow (~5–10 min) — downloads 1.6 GB model weights into the image
docker compose -f docker/docker-compose.yml up --build -d

# Watch logs
docker compose -f docker/docker-compose.yml logs -f api

# Verify all containers healthy
docker ps
# NAMES                  STATUS
# medical_api            Up X minutes (healthy)
# medical_db             Up X minutes (healthy)
# medical_tempo          Up X minutes
# medical_prometheus     Up X minutes
# medical_grafana        Up X minutes
```

### Stop

```bash
docker compose -f docker/docker-compose.yml down
# Preserve DB data
docker compose -f docker/docker-compose.yml down --volumes  # WARNING: deletes data
```

---

## 2. Running the Test Suite

### Install dev dependencies

```bash
pip install -r requirements-dev.txt
# Installs: pytest, pytest-cov
```

### Run all tests

```bash
# From the repo root — pytest.ini auto-discovers tests/ and measures coverage
python -m pytest
```

Expected output (all 29 tests should pass, coverage ≥ 70 %):

```
tests/test_api.py ...........................          [100%]
TOTAL    291    57    80%
Required test coverage of 70% reached. Total coverage: 80.41%
========================== 29 passed in Xs ===========================
```

### Run without coverage (faster during development)

```bash
python -m pytest --no-cov
```

### Run a single test by name

```bash
python -m pytest -k test_predict_returns_503_on_db_error -v
```

### What the tests cover

| Area | Tests |
|------|-------|
| `GET /health` | healthy, degraded DB, configured model metadata |
| `POST /predict` | happy path, optional fields, fallback (all providers failed) |
| `POST /predict` validation | symptoms too short/long, blank, missing `patient_id`, `patient_id` too long, `age` out of range, `notes` too long |
| `POST /predict` errors | 503 on asyncpg DB error, 503 on `OSError` |
| `GET /predict/{id}` | found, not found (404), non-integer ID (422), record with `is_fallback=True` |
| `GET /metrics` | endpoint reachable, Prometheus counter present |
| Service unit | `classify_symptoms` fallback/non-fallback persistence paths, `check_db_health` success/failure |

### Why heavy dependencies are not installed

`conftest.py` stubs `asyncpg`, `transformers`, `torch`, and `opentelemetry.instrumentation.asyncpg` in `sys.modules` before any app module is imported. This lets the test suite run on a plain Python environment without a GPU, model weights, or a running PostgreSQL database. All database and model calls are replaced with `AsyncMock` / `MagicMock` inside individual tests.

---

## 3. Testing the Endpoints (curl)

### Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","db":"healthy","model":"facebook/bart-large-mnli","version":"1.0.0"}
```

### POST /auth/token — obtain a JWT

```bash
curl -X POST http://localhost:8000/auth/token \
  -d "username=admin&password=changeme" \
  -H "Content-Type: application/x-www-form-urlencoded"
```

Response:

```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer"
}
```

### POST /predict — valid input

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "patient_id": "P-042",
    "symptoms": "Patient complains of burning sensation during urination, frequent urge to urinate, lower abdominal pain",
    "age": 26,
    "notes": "No fever. Female patient."
  }'
```

Response:

```json
{
    "record_id": 1,
    "patient_id": "P-042",
    "symptoms": "Patient complains of burning sensation during urination, frequent urge to urinate, lower abdominal pain",
    "top_condition": "Urinary Tract Infection",
    "confidence": 0.85,
    "all_predictions": [
        {
            "label": "Urinary Tract Infection",
            "score": 0.85
        },
        {
            "label": "Kidney Stones",
            "score": 0.12
        }
    ],
    "model_version": "1.0.0",
    "is_fallback": false,
    "fallback_message": null,
    "created_at": "2026-04-11T12:00:00.000Z"
}
```

### POST /predict — fallback response (all providers failed)

When the model is unavailable the API still returns `201`. Check `is_fallback`. See [Section 5. Fallback Behaviour](#5-fallback-behaviour) for the exact JSON payload.

### POST /predict — bad input → 422

```bash
# symptoms too short
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"patient_id":"P-001","symptoms":"pain"}'
# Returns 422 with validation error detail

# blank patient_id
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"patient_id":"","symptoms":"Severe chest pain radiating to left arm, shortness of breath"}'
# Returns 422

# age out of range
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"patient_id":"P-001","symptoms":"Persistent cough and wheezing for 2 weeks, worse at night","age":999}'
# Returns 422
```

### GET /predict/{id} — fetch saved result

```bash
curl http://localhost:8000/predict/1
```

### Swagger UI

```
http://localhost:8000/docs
```

---

## 4. DB Schema

```sql
-- Initial table creation
CREATE TABLE IF NOT EXISTS predictions (
    id              SERIAL PRIMARY KEY,
    patient_id      VARCHAR(64) NOT NULL,
    symptoms        TEXT NOT NULL,
    top_condition   VARCHAR(128),
    confidence      FLOAT,
    all_predictions JSONB,
    model_version   VARCHAR(32),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Migration for columns added later
ALTER TABLE predictions
    ADD COLUMN IF NOT EXISTS age         SMALLINT,
    ADD COLUMN IF NOT EXISTS notes       TEXT,
    ADD COLUMN IF NOT EXISTS is_fallback BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS provider    VARCHAR(32) DEFAULT 'huggingface';
```

To find all fallback records (where no provider could classify):

```sql
SELECT id, patient_id, created_at FROM predictions WHERE is_fallback = TRUE ORDER BY created_at DESC;
```

---

## 5. Fallback Behaviour

There are two distinct failure modes with different HTTP outcomes:

### All providers failed (→ 201 with `is_fallback: true`)

When the provider fallback chain cannot produce a classification (HuggingFace/OpenAI/Gemini all fail or are unavailable), `POST /predict` returns `201 Created`. The response body contains:

```json
{
    "is_fallback": true,
    "top_condition": "unclassifiable",
    "confidence": 0.0,
    "all_predictions": [],
    "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"
}
```

- `GET /health` reports configured model metadata (`"model"` from `MODEL_NAME`, `"version"` from `MODEL_VERSION`); provider readiness is validated through logs/alerts rather than live probes.
- Retry attempts are logged as `WARNING` before each sleep; the final failure is logged as `ERROR`.
- All fallback records are saved to the DB with `is_fallback = TRUE`. Use the query in section 4 to audit them.

### Database unavailable (→ 503)

When the database is unreachable, `POST /predict` returns `503 Service Unavailable` with a structured error body:

```json
{
    "detail": "Database unavailable; the prediction could not be saved. Please retry later."
}
```

A `record_id` requires a successful DB write, so a 201 response is not possible in this case. Check `GET /health` — `"db": "unreachable"` confirms the database is down. The `db_error_on_predict` event is logged at `ERROR` level with the `asyncpg` error detail.

The handler catches `asyncpg.PostgresError` (server-side errors), `asyncpg.InterfaceError` (pool/connection-management errors), and `OSError` / `ConnectionRefusedError` (low-level network failures), so all reachability scenarios produce a 503 rather than an unstructured 500.

---

## 6. Observability

### Reading Structured Logs

All log output is structured JSON by default. To read and filter logs:

```bash
# Pretty-print all logs
docker logs medical_api | python -m json.tool

# With jq (if installed)
docker logs medical_api | jq .

# Filter to a specific trace_id
docker logs medical_api | jq 'select(.trace_id == "abcdef1234...")'

# Show only errors
docker logs medical_api | jq 'select(.level == "error")'
```

Each request emits a log line with these fields:

```json
{
    "timestamp": "2026-04-08T10:00:00Z",
    "level": "info",
    "event": "request_completed",
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "span_id": "00f067aa0ba902b7",
    "method": "POST",
    "path": "/predict",
    "status_code": 201,
    "duration_ms": 312.5
}
```

The `X-Trace-ID` response header carries the same hex trace ID so callers can correlate logs with distributed traces in Grafana Tempo.

Set `LOG_FORMAT=console` in `.env` for human-readable output during local development.

### Checking Prometheus Metrics

```bash
# Raw metrics endpoint
curl http://localhost:8000/metrics
```

Open **Prometheus UI** at http://localhost:9090.

Useful PromQL queries:

```promql
# Request rate over 1 minute (all endpoints)
rate(http_requests_total[1m])

# Request rate broken down by endpoint and method
rate(http_requests_total[1m])

# Error rate (4xx + 5xx) — label is status_code, not status
rate(http_requests_total{status_code=~"4..|5.."}[1m])

# Current process memory in MB
process_rss_bytes / 1024 / 1024

# Current CPU usage (percent)
process_cpu_percent
```

> **Note:** `http_requests_total` carries three labels: `method`, `path`, and `status_code`. Use `{status_code=~"..."}` (not `{status=~"..."}`) when filtering by response code.

### Grafana Dashboards & Distributed Traces (Tempo)

**UI Access:**

- **Grafana**: http://localhost:3000 (default credentials: `admin` / `admin`)
- **Tempo UI / Query**: http://localhost:3200
- **Prometheus UI**: http://localhost:9090

Both the **Prometheus** and **Tempo** datasources are pre-provisioned automatically in Grafana.

To build a metrics dashboard:

1. Click **+** → **New Dashboard** → **Add visualization**
2. Select the **Prometheus** datasource
3. Enter a PromQL query (e.g. `rate(http_requests_total[1m])`)

To explore distributed traces:

1. Click **Explore** (compass icon in the left sidebar)
2. Select the **Tempo** datasource
3. Search by **Trace ID** (copy the `X-Trace-ID` response header or `trace_id` log field) or browse recent traces via **Search**

### Langfuse LLM Traces

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in `.env`, then restart the service. Each call to `POST /predict` creates a trace named `classify_symptoms` with a generation span named `classify_with_fallback`.

- Cloud UI: https://cloud.langfuse.com
- Self-hosted: set `LANGFUSE_HOST` to your instance URL

Traces include: model name, input symptoms, top predicted condition, and approximate token count. If keys are not set, tracing is silently skipped — the service functions normally.

---

## 7. Troubleshooting

| Symptom                                                                     | Cause                                                                                                                                    | Fix                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `api` container unhealthy                                                   | Service not ready yet                                                                                                                    | Wait 30–60 s, check `docker logs medical_api`                                                                                                                                                                                                                                                                                                                   |
| `medical_db is unhealthy` + warning `POSTGRES_PASSWORD variable is not set` | Docker Compose v2 reads `.env` from the project directory, which defaults to the folder of the `-f` file (`docker/`) — not the repo root | Ensure `docker-compose.yml` has `env_file: - ../.env` on both services (already fixed); alternatively run with `--env-file .env`                                                                                                                                                                                                                                |
| `/predict` returns `is_fallback: true`                                      | Model unavailable or inference keeps failing                                                                                             | `/health` is metadata-only for model info; validate runtime failures via logs: `docker logs medical_api \| jq 'select(.event == "provider_failed" or .event == "all_providers_failed")'`.                                                                                                                                                                      |
| `/predict` returns `503`                                                    | Database unreachable at request time                                                                                                     | Check `/health` → `"db": "unreachable"`; filter logs for `db_error_on_predict`. Verify `medical_db` is running (`docker ps`) and wait for `(healthy)`.                                                                                                                                                                                                          |
| `docker build` fails at model download step                                 | No internet access during build                                                                                                          | Build requires internet access once to fetch `facebook/bart-large-mnli` (~1.6 GB)                                                                                                                                                                                                                                                                               |
| `db` connection refused                                                     | Postgres not ready                                                                                                                       | `docker ps` → wait for `(healthy)` on `medical_db`                                                                                                                                                                                                                                                                                                              |
| 422 on valid-looking input                                                  | `symptoms` < 10 chars                                                                                                                    | Minimum 10 characters required                                                                                                                                                                                                                                                                                                                                  |
| Port 8000 already in use                                                    | Another service on port                                                                                                                  | `lsof -i :8000`, kill it, or change port in compose                                                                                                                                                                                                                                                                                                             |
| `http_requests_total` query returns no results in Prometheus                | Metric was not defined — OTel FastAPI instrumentation generates histograms with OTel-convention names, not this counter                  | Fixed: `RequestLoggingMiddleware` now increments an explicit `prometheus_client.Counter`. Make at least one request to the API first; the counter appears only after the first increment. Use `{status_code=~"4..\|5.."}` (not `status=~`) to filter by response code. |
| `GET /metrics` returns 404                                                  | `/metrics` route not mounted                                                                                                             | Verify `app.mount("/metrics", make_metrics_app())` is present in `main.py` and `setup_telemetry()` was called first                                                                                                                                                                                                                                             |
| `X-Trace-ID` header missing from response                                   | OTel span not active — `instrument_app` called inside `lifespan` too late                                                                | `configure_logging()`, `setup_telemetry()`, and `FastAPIInstrumentor.instrument_app(app)` must be at **module level** in `main.py`, not inside `lifespan`. Starlette freezes the middleware stack before lifespan runs; anything added inside lifespan is never part of the live pipeline.                                                                      |
| `medical_tempo` exits immediately on start                                  | Named volume owned by root; Tempo UID 10001 has no write access                                                                          | Volume must mount to `/var/tempo` (not `/tmp/tempo`) — the path the Tempo 2.5.0 image pre-owns as `tempo:tempo`. Confirm `docker-compose.yml` has `tempo_data:/var/tempo` and `tempo.yaml` uses `/var/tempo/blocks` and `/var/tempo/wal`. If you have an old `tempo_data` volume from a previous run, destroy it first: `docker volume rm <project>_tempo_data` |
| Tempo not receiving traces                                                  | OTLP endpoint unreachable                                                                                                                | Check `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env`; must be a base URL — inside Compose use `http://tempo:4318`, outside Compose use `http://localhost:4318` (the SDK appends `/v1/traces` automatically); verify `medical_tempo` container is running                                                                                                               |
| Prometheus shows `medical_api` target as DOWN                               | DNS resolution fails inside Compose network                                                                                              | Ensure the target in `prometheus.yml` is `api:8000` (the Compose service name), not `localhost:8000`                                                                                                                                                                                                                                                            |
| Logs are printed as plain text, not JSON                                    | `LOG_FORMAT` not set to `json`                                                                                                           | Set `LOG_FORMAT=json` in `.env` and restart                                                                                                                                                                                                                                                                                                                     |
| Langfuse traces not appearing                                               | Keys missing or wrong host                                                                                                               | Verify `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set; check for `langfuse_init_failed` event in logs                                                                                                                                                                                                                                                    |
| `POST /predict` returns `401 Missing authentication credentials`            | `API_KEYS` is set in env but neither `X-API-Key` nor `Authorization: Bearer ...` was sent                                              | Add either `-H "X-API-Key: <your-key>"` **or** `-H "Authorization: Bearer <token>"`; see Section 8 for full auth guide                                                                                                                                                                                                                                          |
| `POST /predict` returns `429 Too Many Requests`                             | Rate limit (30 req/min/IP) exceeded                                                                                                     | Back off and retry after 60 s; or increase `slowapi` limit in `app/routers/api.py` if needed                                                                                                                                                                                                                                                                    |
| AlertManager container fails to start                                       | `docker/alertmanager.yml` is missing (it is gitignored)                                                                                 | Copy `docker/alertmanager.yml.example` → `docker/alertmanager.yml` and fill in the Slack webhook URL                                                                                                                                                                                                                                                            |
| Alerts fire in Prometheus but no Slack message arrives                      | Slack webhook URL still set to placeholder                                                                                               | Open `docker/alertmanager.yml`, replace `https://hooks.slack.com/services/REPLACE/...` with the real URL, restart AlertManager: `docker compose -f docker/docker-compose.yml restart alertmanager`                                                                                                                                                              |
| Alert rules not reloading after editing `docker/prometheus/alerts.yml`      | Prometheus requires explicit reload                                                                                                     | `curl -X POST http://localhost:9090/-/reload` (works because `--web.enable-lifecycle` flag is set); alternatively restart: `docker compose -f docker/docker-compose.yml restart prometheus`                                                                                                                                                                      |
| `is_fallback: true` even with `OPENAI_API_KEY` set                          | Circuit breaker may be open after 3 consecutive failures                                                                                 | Check logs for `provider_circuit_open` events; the breaker resets after 60 s automatically. Verify the key is valid by calling the OpenAI API directly.                                                                                                                                                                                                         |

---

## 8. Authentication Guide

### X-API-Key (recommended for service-to-service)

Set one or more keys in `.env`:

```
API_KEYS=my-secret-key-1,my-secret-key-2
```

Include the header on every protected request:

```bash
curl -X POST http://localhost:8000/predict \
  -H "X-API-Key: my-secret-key-1" \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "p001", "symptoms": "headache and fever for 3 days"}'
```

When `API_KEYS` is empty/unset, auth is disabled (dev mode). The `/health` and `/metrics` endpoints are always public.
When `API_KEYS` is set, protected endpoints accept **either** a valid `X-API-Key` **or** a valid Bearer JWT.

### JWT Bearer Token (for user-facing clients)

1. Obtain a token:

```bash
curl -X POST http://localhost:8000/auth/token \
  -d "username=admin&password=your-admin-password" \
  -H "Content-Type: application/x-www-form-urlencoded"
# Returns: {"access_token": "eyJ...", "token_type": "bearer"}
```

2. Use the token:

```bash
curl -X POST http://localhost:8000/predict \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{"patient_id": "p001", "symptoms": "headache and fever for 3 days"}'
```

Tokens expire after `JWT_EXPIRE_MINUTES` (default 60). Set a strong `JWT_SECRET_KEY` in production (`openssl rand -hex 32`).

---

## 9. Multi-Provider Fallback

The service tries providers in order. Configure credentials to enable each:

| Provider | Env var | Notes |
|----------|---------|-------|
| HuggingFace (local) | _(always available)_ | `facebook/bart-large-mnli` baked into image |
| OpenAI | `OPENAI_API_KEY` | Model set by `OPENAI_MODEL` (default: `gpt-4o-mini`) |
| Gemini | `GEMINI_API_KEY` | Uses `gemini-2.0-flash` via REST API |

If all providers fail, the response includes `is_fallback: true` and `fallback_message`.

Each provider has a circuit breaker (3 failures → open for 60 s). Monitor via the `HighFallbackRate` alert and the `medical_ai_predictions_total` metric with `provider` label.

---

## 10. Alerting Setup

1. Copy the AlertManager config: `cp docker/alertmanager.yml.example docker/alertmanager.yml`
2. Edit `docker/alertmanager.yml` — replace the placeholder Slack webhook URLs with real ones.
3. Start the stack: `docker compose -f docker/docker-compose.yml up -d alertmanager`
4. Verify in Prometheus UI (http://localhost:9090/alerts) that alerts are in `INACTIVE` state.

To reload alert rules without restarting Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
```

AlertManager UI is at http://localhost:9093.
