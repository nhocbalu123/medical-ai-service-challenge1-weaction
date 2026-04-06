# RUNBOOK.md — Operations Guide

## Service: Medical Symptom Classifier API

---

## 1. Running the Service

### Prerequisites
- Docker Desktop (or Docker Engine + Compose plugin)
- 4 GB RAM minimum (for HuggingFace model)
- ~3 GB free disk space (model weights are baked into the image at build time)
- Port 8000 and 5432 free
- Internet access during `docker build` (to download `facebook/bart-large-mnli` weights once)

### Start

```bash
# From repo root
# First build is slow (~5–10 min) — downloads 1.6 GB model weights into the image
docker compose -f docker/docker-compose.yml up --build -d

# Watch logs
docker compose -f docker/docker-compose.yml logs -f api

# Verify both healthy
docker ps
# NAMES          STATUS
# medical_api    Up X minutes (healthy)
# medical_db     Up X minutes (healthy)
```

### Stop

```bash
docker compose -f docker/docker-compose.yml down
# Preserve DB data
docker compose -f docker/docker-compose.yml down --volumes  # WARNING: deletes data
```

---

## 2. Testing the Endpoints

### Health check
```bash
curl http://localhost:8000/health
# {"status":"ok","db":"healthy","model":"loaded","version":"1.0.0"}
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

## 3. DB Schema

```sql
CREATE TABLE predictions (
    id            SERIAL PRIMARY KEY,
    patient_id    VARCHAR(64) NOT NULL,
    symptoms      TEXT NOT NULL,
    top_condition VARCHAR(128),
    confidence    FLOAT,
    all_predictions JSONB,
    model_version VARCHAR(32),
    age           SMALLINT,
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `api` container unhealthy | Service not ready yet | Wait 30–60 s, check `docker logs medical_api` |
| `medical_db is unhealthy` + warning `POSTGRES_PASSWORD variable is not set` | Docker Compose v2 reads `.env` from the project directory, which defaults to the folder of the `-f` file (`docker/`) — not the repo root | Ensure `docker-compose.yml` has `env_file: - ../.env` on both services (already fixed); alternatively run with `--env-file .env` |
| `/predict` returns 503 Service Unavailable | Model failed to load | Check `/health` endpoint — if `"model": "unavailable"`, see logs: `docker logs medical_api \| grep "Model load failed"` |
| `docker build` fails at model download step | No internet access during build | Build requires internet access once to fetch `facebook/bart-large-mnli` (~1.6 GB) |
| `db` connection refused | Postgres not ready | `docker ps` → wait for `(healthy)` on `medical_db` |
| 422 on valid-looking input | `symptoms` < 10 chars | Minimum 10 characters required |
| Port 8000 already in use | Another service on port | `lsof -i :8000`, kill it, or change port in compose |
