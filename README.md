# 🏥 Medical Symptom Classifier API

### `medical-ai-service-challenge1-weaction`

A production-ready **FastAPI** standalone backend service, built as a monolith, organized in a layered architecture, that wraps a multi-provider AI classification chain (HuggingFace → OpenAI → Gemini) to predict likely medical conditions from free-text symptom descriptions. All predictions are persisted to **PostgreSQL** for tracking and audit.

> ⚠️ **Disclaimer:** This service is for educational/demo purposes only. It is NOT a substitute for professional medical diagnosis or advice.

---

## 📑 Table of Contents

- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Quick Start (Docker Compose)](#-quick-start-docker-compose)
- [Project Structure](#-project-structure)
- [Observability](#-observability)
- [API Endpoints](#-api-endpoints)
- [Environment Variables](#-environment-variables)
- [Common Mistakes Avoided](#-common-mistakes-avoided)

---

## 📐 Architecture

```
POST /predict   →  Pydantic validation  →  HF zero-shot classifier (3 total attempts)
                                         └─ on fail: OpenAI (gpt-4o-mini)
                                            └─ on fail: Gemini (gemini-2.0-flash)
                                               └─ on fail: Fallback (is_fallback=true)
                                                     ↓
                                             save to Postgres
                                                     ↓
                                                return JSON

GET  /predict/{id}  →  fetch from Postgres  →  return JSON
GET  /health    →  check DB + expose model metadata  →  return JSON
```

**Tech stack:** FastAPI · Pydantic v2 · asyncpg · HuggingFace Transformers · OpenAI · Google Gemini · PostgreSQL 16 · Docker multi-stage · structlog · OpenTelemetry · Grafana Tempo · Prometheus · Langfuse

**Features:** Multi-provider AI fallback chain, Rate Limiting (`slowapi`), CORS, Security Headers, Authentication (JWT/API Key), Structured Logging, Distributed Tracing, Prometheus Metrics.

---

## 📋 Prerequisites

- **Docker Desktop** (or Docker Engine + Compose plugin)
- **4 GB RAM minimum** (for HuggingFace model)
- **~3 GB free disk space** (model weights are baked into the image at build time)
- Ports **8000** (API), **9090** (Prometheus), **3000** (Grafana), **4318** (Tempo OTLP ingest), and **3200** (Tempo query API) free on the host. PostgreSQL runs inside the Docker network only.
- Internet access during `docker build` (to download `facebook/bart-large-mnli` weights once)

---

## 🚀 Quick Start (Docker Compose)

```bash
git clone https://github.com/nhocbalu123/medical-ai-service-challenge1-weaction.git
cd medical-ai-service-challenge1-weaction

# Supply required secrets (POSTGRES_PASSWORD is mandatory — no default)
cp .env.example .env
# Edit .env and set POSTGRES_PASSWORD to a strong value before continuing

# Build and start all services (api + db + prometheus + grafana)
# Note: first build takes ~5–10 min — downloads facebook/bart-large-mnli (~1.6 GB) into the image
docker compose -f docker/docker-compose.yml up --build -d

# Check containers are healthy
docker ps

# Health check
curl http://localhost:8000/health

# Swagger UI
open http://localhost:8000/docs
```

For detailed API usage examples (including `curl` commands and fallback behaviors), please see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## 📁 Project Structure

```
medical-ai-service/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, middleware, OTel setup, /metrics mount
│   ├── core/
│   │   ├── auth.py                # JWT token generation and validation
│   │   ├── constants.py           # Shared constants (e.g., CONDITION_LABELS)
│   │   ├── limiter.py             # Rate limiting setup (slowapi)
│   │   ├── logging_config.py      # structlog JSON/console setup + OTel context processor
│   │   ├── security.py            # API key and JWT authentication dependencies
│   │   └── telemetry.py           # OTel trace/metric providers, asyncpg instrumentation
│   ├── middleware/logging.py      # Request duration/status logging + X-Trace-ID header
│   ├── routers/
│   │   ├── api.py                 # 3 endpoints: POST /predict, GET /predict/{id}, GET /health
│   │   └── auth.py                # POST /auth/token endpoint
│   ├── models/schemas.py          # Pydantic request/response models
│   ├── services/
│   │   ├── core.py                # asyncpg DB + orchestration + Langfuse tracing
│   │   └── providers.py           # AI providers (HuggingFace, OpenAI, Gemini) + fallback logic
├── docker/
│   ├── Dockerfile                 # Multi-stage build (python:3.11-slim)
│   ├── docker-compose.yml         # api + db + tempo + prometheus + grafana + alertmanager
│   ├── tempo.yaml                 # Grafana Tempo config (OTLP receiver, local storage)
│   ├── prometheus.yml             # Prometheus scrape config + alert rules loading
│   ├── alertmanager.yml.example   # Alertmanager config template (copy to alertmanager.yml)
│   ├── prometheus/
│   │   └── alerts.yml             # Prometheus alert rules (5 rules)
│   └── grafana/provisioning/      # Auto-provisions Prometheus + Tempo datasources in Grafana
├── docs/
│   ├── RUNBOOK.md                 # Detailed ops guide + troubleshooting
│   ├── CHANGELOG.md               # Version history
│   └── AVOIDANCE_TABLE.md         # Real issues encountered and resolved
├── tests/
│   ├── conftest.py                # Test fixtures and dependency stubs
│   └── test_api.py                # API smoke tests
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── .dockerignore
└── README.md
```

---

## 📊 Observability

The service is fully instrumented out of the box.

### Structured Logging & Tracing

All log output is **structured JSON** by default. Every HTTP request automatically emits a log line with `trace_id`, `span_id`, `method`, `path`, `status_code`, and `duration_ms`. The `X-Trace-ID` response header carries the same hex trace ID so callers can correlate logs with distributed traces in Grafana Tempo.

For an example of the structured log payload, see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

### Metrics (`/metrics`)

Prometheus-format metrics are exposed at `GET /metrics`. Key metrics:

| Metric                          | Description                                    |
| ------------------------------- | ---------------------------------------------- |
| `http_requests_total`           | Request count by method, path, and status code |
| `http_server_duration_milliseconds_bucket` | Request latency histogram (OTel)    |
| `process_cpu_percent`           | Current process CPU %                          |
| `process_rss_bytes`             | Current process RSS memory in bytes            |

### Local Observability Demo

The `docker-compose.yml` includes a pre-configured **Tempo + Prometheus + Grafana** stack for local development. After `docker compose up`:

| UI | URL | Credentials |
|----|-----|-------------|
| Tempo | http://localhost:3200 | — |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | admin / admin |

Grafana starts with both the Prometheus and Tempo datasources pre-provisioned.

### LLM Tracing (Langfuse)

Each inference call is traced as a **Langfuse generation**. Tracing is opt-in and disabled when `LANGFUSE_PUBLIC_KEY` is not set.

### Alerting (Prometheus & Alertmanager)

The service includes **Prometheus alert rules** and **Alertmanager** for proactive monitoring and incident notification.

| Alert | Severity | Condition | Impact |
|-------|----------|-----------|--------|
| `HighErrorRate` | critical | 5xx error rate > 5% over 5 minutes | Service degradation detected |
| `SlowResponses` | warning | P95 response latency > 10 seconds | Performance degradation |
| `HighFallbackRate` | critical | Fallback predictions > 20% over 10 minutes | Model or inference issues |
| `HighMemoryUsage` | warning | Process RSS > 3.4 GB | Potential OOM risk |
| `HighCPUUsage` | warning | Process CPU > 90% | Resource exhaustion |

For full setup and troubleshooting details (including Slack webhook configuration), see [`docs/RUNBOOK.md`](docs/RUNBOOK.md) (Section 10. Alerting Setup).

---

## 🔌 API Endpoints

| Method | Path            | Description                                                                                                                                                                |
| ------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST` | `/predict`      | Submit symptoms → get AI prediction + saved to DB. Returns `201` when all models are unavailable (check `is_fallback`); returns `503` only when the database is unreachable. |
| `GET`  | `/predict/{id}` | Retrieve a saved prediction by record ID                                                                                                                                   |
| `GET`  | `/health`       | Live status of API/DB plus configured model name (`MODEL_NAME`) and model version (`MODEL_VERSION`)                                                                       |
| `POST` | `/auth/token`   | Obtain a JWT access token using `username` and `password`                                                                                                                  |
| `GET`  | `/metrics`      | Prometheus metrics endpoint                                                                                                                                                |

For detailed fallback behavior (including JSON payloads) and DB-down behavior, see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

Full interactive docs: **`http://localhost:8000/docs`**

---

## 🌍 Environment Variables

Copy `.env.example` to `.env` and fill in the required values before running.

| Variable                      | Required | Default                      | Description                                                                                                                 |
| ----------------------------- | -------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `POSTGRES_PASSWORD`           | **yes**  | —                            | Password for the PostgreSQL `postgres` user                                                                                 |
| `POSTGRES_DB`                 | no       | `medicaldb`                  | PostgreSQL database name                                                                                                    |
| `POSTGRES_USER`               | no       | `postgres`                   | PostgreSQL user                                                                                                             |
| `POSTGRES_HOST`               | no       | `localhost`                  | PostgreSQL hostname (overridden to `db` inside Docker Compose)                                                              |
| `POSTGRES_PORT`               | no       | `5432`                       | PostgreSQL port                                                                                                             |
| `DATABASE_URL`                | no       | _(built from above)_         | Full Postgres DSN; overrides `POSTGRES_*` vars when set                                                                     |
| `API_KEYS`                    | no       | —                            | Comma-separated list of valid API keys for authenticating `/predict` endpoints. If empty, auth is disabled.                 |
| `JWT_SECRET_KEY`              | no       | `insecure-dev-secret...`     | Secret key for signing JWT tokens                                                                                           |
| `JWT_EXPIRE_MINUTES`          | no       | `60`                         | Expiration time for JWT tokens in minutes                                                                                   |
| `ADMIN_USERNAME`              | no       | `admin`                      | Username for obtaining JWT token                                                                                            |
| `ADMIN_PASSWORD`              | no       | `changeme`                   | Password for obtaining JWT token                                                                                            |
| `ALLOWED_ORIGINS`             | no       | —                            | Comma-separated list of allowed origins for CORS. If empty, all cross-origin requests are disallowed.                       |
| `MODEL_NAME`                  | no       | `facebook/bart-large-mnli`   | HuggingFace model ID (requires Docker image rebuild if changed)                                                             |
| `MODEL_VERSION`               | no       | `1.0.0`                      | Version string surfaced in prediction responses                                                                             |
| `OPENAI_API_KEY`              | no       | —                            | API key for OpenAI fallback model                                                                                           |
| `OPENAI_MODEL`                | no       | `gpt-4o-mini`                | Model ID for OpenAI fallback                                                                                                |
| `GEMINI_API_KEY`              | no       | —                            | API key for Google Gemini fallback model                                                                                    |
| `LOG_FORMAT`                  | no       | `json`                       | Log output format: `json` (machine-readable) or `console` (human-readable)                                                  |
| `LOG_LEVEL`                   | no       | `INFO`                       | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`                                                                          |
| `LANGFUSE_PUBLIC_KEY`         | no       | —                            | Langfuse public key; tracing disabled when not set                                                                          |
| `LANGFUSE_SECRET_KEY`         | no       | —                            | Langfuse secret key                                                                                                         |
| `LANGFUSE_HOST`               | no       | —                            | Langfuse host URL; omit for cloud.langfuse.com                                                                              |
| `GRAFANA_USER`                | no       | `admin`                      | Grafana admin username (local demo stack only)                                                                              |
| `GRAFANA_PASSWORD`            | no       | `admin`                      | Grafana admin password (local demo stack only)                                                                              |
| `OTEL_SERVICE_NAME`           | no       | `medical-ai-service`         | Service name reported in traces and metrics                                                                                 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no       | `http://tempo:4318`          | OTLP/HTTP **base URL** for trace export (SDK auto-appends `/v1/traces`); use `http://localhost:4318` outside Docker Compose |
| `OTEL_RESOURCE_ATTRIBUTES`    | no       | `deployment.environment=dev` | Extra resource attributes (key=value pairs) attached to every span and metric                                               |

> The service **refuses to start** if neither `DATABASE_URL` nor `POSTGRES_PASSWORD` is set.
>
> `DATABASE_URL` is built automatically at runtime from the `POSTGRES_*` vars — you do not need to set it manually when using Docker Compose.

---

## ✅ Common Mistakes Avoided

See **`docs/AVOIDANCE_TABLE.md`** and **`docs/RUNBOOK.md`** for full details on historical context, troubleshooting, and operational tasks.
