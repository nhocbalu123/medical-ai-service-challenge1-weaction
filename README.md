# 🏥 Medical Symptom Classifier API
### `medical-ai-service-challenge1-weaction`

A production-ready **FastAPI** standalone backend service, built as a monolith, organized in a layered architecture, that wraps a HuggingFace zero-shot classification model to predict likely medical conditions from free-text symptom descriptions. All predictions are persisted to **PostgreSQL** for tracking and audit.

> ⚠️ **Disclaimer:** This service is for educational/demo purposes only. It is NOT a substitute for professional medical diagnosis or advice.

---

## 📐 Architecture

```
POST /predict   →  Pydantic validation  →  HF zero-shot classifier (retry ×3)  →  save to Postgres  →  return JSON
                                         └─ fallback if model unavailable ──────┘  (is_fallback=true)
GET  /predict/{id}  →  fetch from Postgres  →  return JSON
GET  /health    →  check DB + model status  →  return JSON
```

**Tech stack:** FastAPI · Pydantic v2 · asyncpg · HuggingFace Transformers · PostgreSQL 16 · Docker multi-stage · structlog · Prometheus · Langfuse

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
docker compose -f docker/docker-compose.yml up --build

# Check containers are healthy
docker ps

# Health check
curl http://localhost:8000/health

# Predict
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"patient_id":"P-001","symptoms":"Severe headache, high fever 39C, stiff neck and sensitivity to light for 2 days","age":28}'

# Bad input → 422
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"patient_id":"","symptoms":"hi"}'

# Swagger UI
open http://localhost:8000/docs
```

---

## 📁 Project Structure

```
medical-ai-service/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, middleware, Prometheus setup
│   ├── core/logging_config.py     # structlog JSON/console setup
│   ├── middleware/logging.py      # Request ID, duration, status logging
│   ├── routers/api.py             # 3 endpoints: POST /predict, GET /predict/{id}, GET /health
│   ├── models/schemas.py          # Pydantic request/response models
│   └── services/core.py           # HuggingFace classifier + asyncpg DB + Langfuse tracing
├── docker/
│   ├── Dockerfile                 # Multi-stage build (python:3.11-slim)
│   ├── docker-compose.yml         # api + db + prometheus + grafana
│   ├── prometheus.yml             # Prometheus scrape config
│   └── grafana/provisioning/      # Auto-provisions Prometheus datasource in Grafana
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

### Structured Logging

All log output is **structured JSON** by default (set `LOG_FORMAT=console` for human-readable output during local development). Every log line includes a `timestamp`, `level`, `logger`, and any structured fields relevant to the event.

Every HTTP request automatically emits a log line with `request_id`, `method`, `path`, `status_code`, and `duration_ms`. The `X-Request-ID` response header carries the same ID so callers can correlate logs.

```json
{"timestamp": "2026-04-08T10:00:00Z", "level": "info", "event": "request_completed",
 "request_id": "3fa85f64-...", "method": "POST", "path": "/predict",
 "status_code": 201, "duration_ms": 312.5}
```

### Metrics (`/metrics`)

Prometheus-format metrics are exposed at `GET /metrics`. Key metrics:

| Metric | Description |
|--------|-------------|
| `http_requests_total` | Request count by method, path, and status code |
| `http_request_duration_seconds` | Request latency histogram |
| `process_cpu_percent` | Current process CPU % |
| `process_rss_bytes` | Current process RSS memory in bytes |

### Local Observability Demo

The `docker-compose.yml` includes a pre-configured **Prometheus + Grafana** stack for local development. After `docker compose up`:

| UI | URL | Credentials |
|----|-----|-------------|
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | admin / admin |

Grafana starts with the Prometheus datasource pre-provisioned. Create a new dashboard and query `http_requests_total` or `http_request_duration_seconds` to get started.

> In production, Prometheus and Grafana live in a shared ops/infra stack. The service itself only cares about exposing `/metrics`; the docker-compose containers are a demo convenience.

### LLM Tracing (Langfuse)

Each inference call is traced as a **Langfuse generation** with the model name, input symptoms, top predicted condition, and latency. Tracing is opt-in and disabled when `LANGFUSE_PUBLIC_KEY` is not set.

To enable:
1. Sign up at [cloud.langfuse.com](https://cloud.langfuse.com) (free tier available) or self-host Langfuse.
2. Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optionally `LANGFUSE_HOST` in your `.env`.
3. Restart the service — traces appear in the Langfuse dashboard immediately.

---

## 🔌 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/predict` | Submit symptoms → get AI prediction + saved to DB. Always returns `201`; check `is_fallback` when the model is unavailable. |
| `GET`  | `/predict/{id}` | Retrieve a saved prediction by record ID |
| `GET`  | `/health` | Live status of API, DB, and model |
| `GET`  | `/metrics` | Prometheus metrics endpoint |

### Fallback behaviour

If the HuggingFace model fails to load **or** inference fails after 3 retry attempts, `POST /predict` still returns `201 Created` — it never returns `503`. The response body includes:

```json
{
  "is_fallback": true,
  "top_condition": "unclassifiable",
  "confidence": 0.0,
  "all_predictions": [],
  "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"
}
```

Callers should check `is_fallback` and display the `fallback_message` to the user. All fallback records are persisted to the database with `is_fallback = TRUE` for audit purposes.

Full interactive docs: **`http://localhost:8000/docs`**

---

## 🌍 Environment Variables

Copy `.env.example` to `.env` and fill in the required values before running.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POSTGRES_PASSWORD` | **yes** | — | Password for the PostgreSQL `postgres` user |
| `POSTGRES_DB` | no | `medicaldb` | PostgreSQL database name |
| `POSTGRES_USER` | no | `postgres` | PostgreSQL user |
| `POSTGRES_HOST` | no | `db` | PostgreSQL hostname (`db` inside Compose network) |
| `POSTGRES_PORT` | no | `5432` | PostgreSQL port |
| `DATABASE_URL` | no | *(built from above)* | Full Postgres DSN; overrides `POSTGRES_*` vars when set |
| `MODEL_NAME` | no | `facebook/bart-large-mnli` | HuggingFace model ID |
| `MODEL_VERSION` | no | `1.0.0` | Version string surfaced in prediction responses |
| `LOG_FORMAT` | no | `json` | Log output format: `json` (machine-readable) or `console` (human-readable) |
| `LOG_LEVEL` | no | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LANGFUSE_PUBLIC_KEY` | no | — | Langfuse public key; tracing disabled when not set |
| `LANGFUSE_SECRET_KEY` | no | — | Langfuse secret key |
| `LANGFUSE_HOST` | no | — | Langfuse host URL; omit for cloud.langfuse.com |
| `GRAFANA_USER` | no | `admin` | Grafana admin username (local demo stack only) |
| `GRAFANA_PASSWORD` | no | `admin` | Grafana admin password (local demo stack only) |

> The service **refuses to start** if neither `DATABASE_URL` nor `POSTGRES_PASSWORD` is set.
>
> `DATABASE_URL` is built automatically at runtime from the `POSTGRES_*` vars — you do not need to set it manually when using Docker Compose.

---

## ✅ Common Mistakes Avoided

See **`docs/AVOIDANCE_TABLE.md`** and **`docs/RUNBOOK.md`** for full details.
