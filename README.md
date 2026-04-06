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
git clone https://github.com/YOUR_USERNAME/medical-ai-service-challenge1-weaction.git
cd medical-ai-service-challenge1-weaction

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

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:postgres@db:5432/medicaldb` | Full Postgres connection string |
| `MODEL_NAME` | `facebook/bart-large-mnli` | HuggingFace model ID |
| `MODEL_VERSION` | `1.0.0` | Displayed in predictions |
| `POSTGRES_PASSWORD` | `postgres` | DB password |
| `POSTGRES_DB` | `medicaldb` | DB name |

---

## ✅ Common Mistakes Avoided

See **`docs/AVOIDANCE_TABLE.md`** and **`docs/RUNBOOK.md`** for full details. Quick summary:

1. `python:3.11-slim` + multi-stage build (small image)
2. No hardcoded secrets — all via `os.getenv()`
3. Pydantic v2 validation with 422 on bad input
4. Router/service/schema separation (no monolith)
5. DB healthcheck before API starts
6. Non-root container user
7. Dockerfile `HEALTHCHECK` directive
8. Model weights pre-downloaded at build time (no runtime internet dependency); graceful mock-mode fallback if load still fails
