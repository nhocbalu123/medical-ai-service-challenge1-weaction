# 🏥 Medical Symptom Classifier API
### `medical-ai-service-challenge1-weaction`

A production-ready **FastAPI** microservice that wraps a HuggingFace zero-shot classification model to predict likely medical conditions from free-text symptom descriptions. All predictions are persisted to **PostgreSQL** for tracking and audit.

> ⚠️ **Disclaimer:** This service is for educational/demo purposes only. It is NOT a substitute for professional medical diagnosis or advice.

---

## 📐 Architecture

```
POST /predict   →  Pydantic validation  →  HF zero-shot classifier  →  save to Postgres  →  return JSON
GET  /predict/{id}  →  fetch from Postgres  →  return JSON
GET  /health    →  check DB + model status  →  return JSON
```

**Tech stack:** FastAPI · Pydantic v2 · asyncpg · HuggingFace Transformers · PostgreSQL 16 · Docker multi-stage

---

## 🚀 Quick Start (Docker Compose)

```bash
git clone https://github.com/nhocbalu123/medical-ai-service-challenge1-weaction.git
cd medical-ai-service-challenge1-weaction

# Supply required secrets (POSTGRES_PASSWORD is mandatory — no default)
cp .env.example .env
# Edit .env and set POSTGRES_PASSWORD to a strong value before continuing

# Build and start both services (api + db)
# Note: first build takes ~5–10 min — downloads facebook/bart-large-mnli (~1.6 GB) into the image
docker compose -f docker/docker-compose.yml up --build

# Check both containers are healthy
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
│   ├── main.py             # FastAPI app + router registration
│   ├── routers/api.py      # 3 endpoints: POST /predict, GET /predict/{id}, GET /health
│   ├── models/schemas.py   # Pydantic request/response models
│   └── services/core.py    # HuggingFace classifier + asyncpg DB logic
├── docker/
│   ├── Dockerfile          # Multi-stage build (python:3.11-slim)
│   └── docker-compose.yml  # api + db, healthchecks, env vars
├── docs/
│   ├── RUNBOOK.md          # Detailed ops guide + troubleshooting
│   └── AVOIDANCE_TABLE.md  # Real issue: classifier randomness + solution
├── utils/                  # Screenshots as proof of working service
├── requirements.txt
├── .dockerignore
└── README.md
```

---

## 🔌 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/predict` | Submit symptoms → get AI prediction + saved to DB |
| `GET`  | `/predict/{id}` | Retrieve a saved prediction by record ID |
| `GET`  | `/health` | Live status of API, DB, and model |

Full interactive docs: **`http://localhost:8000/docs`**

---

## 🌍 Environment Variables

Copy `.env.example` to `.env` and fill in the required values before running.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POSTGRES_PASSWORD` | **yes** | — | Password for the PostgreSQL `postgres` user |
| `POSTGRES_DB` | no | `medicaldb` | PostgreSQL database name |
| `POSTGRES_USER` | no | `postgres` | PostgreSQL user |
| `POSTGRES_HOST` | no | `db` | PostgreSQL hostname (set to `db` by Compose for the internal network) |
| `POSTGRES_PORT` | no | `5432` | PostgreSQL port |
| `DATABASE_URL` | no | *(built from above)* | Full Postgres connection string; overrides the individual `POSTGRES_*` vars when set |
| `MODEL_NAME` | no | `facebook/bart-large-mnli` | HuggingFace model ID |
| `MODEL_VERSION` | no | `1.0.0` | Version string surfaced in prediction responses |

> The service **refuses to start** if neither `DATABASE_URL` nor `POSTGRES_PASSWORD` is set.
>
> `DATABASE_URL` is built automatically at runtime from the `POSTGRES_*` vars — you do not need to set it manually when using Docker Compose.

---

## ✅ Common Mistakes Avoided

See **`docs/AVOIDANCE_TABLE.md`** and **`docs/RUNBOOK.md`** for full details.
