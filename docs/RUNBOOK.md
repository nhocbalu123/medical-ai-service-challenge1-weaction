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

### POST /predict — fallback response (model unavailable)

When the model is unavailable the API still returns `201`. Check `is_fallback`:

```bash
# Example response when model failed to load or all retries exhausted:
# {
#   "record_id": 7,
#   "patient_id": "P-042",
#   "top_condition": "unclassifiable",
#   "confidence": 0.0,
#   "all_predictions": [],
#   "is_fallback": true,
#   "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"
# }
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
    id              SERIAL PRIMARY KEY,
    patient_id      VARCHAR(64) NOT NULL,
    symptoms        TEXT NOT NULL,
    top_condition   VARCHAR(128),
    confidence      FLOAT,
    all_predictions JSONB,
    model_version   VARCHAR(32),
    age             SMALLINT,
    notes           TEXT,
    is_fallback     BOOLEAN DEFAULT FALSE,  -- TRUE when model was unavailable or all retries failed
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

To find all fallback records (where the model could not classify):

```sql
SELECT id, patient_id, created_at FROM predictions WHERE is_fallback = TRUE ORDER BY created_at DESC;
```

---

## 4. Fallback Behaviour

When the HuggingFace model cannot classify (model failed to load, or inference fails after 3 retry attempts), `POST /predict` returns `201 Created` — **not** `503`. The response body contains:

```json
{
  "is_fallback": true,
  "top_condition": "unclassifiable",
  "confidence": 0.0,
  "all_predictions": [],
  "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"
}
```

- Check `GET /health` — `"model": "unavailable"` confirms the model failed to load.
- Retry attempts are logged as `WARNING` before each sleep; the final failure is logged as `ERROR`.
- All fallback records are saved to the DB with `is_fallback = TRUE`. Use the query in section 3 to audit them.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `api` container unhealthy | Service not ready yet | Wait 30–60 s, check `docker logs medical_api` |
| `medical_db is unhealthy` + warning `POSTGRES_PASSWORD variable is not set` | Docker Compose v2 reads `.env` from the project directory, which defaults to the folder of the `-f` file (`docker/`) — not the repo root | Ensure `docker-compose.yml` has `env_file: - ../.env` on both services (already fixed); alternatively run with `--env-file .env` |
| `/predict` returns `is_fallback: true` | Model unavailable or inference keeps failing | Check `/health` → `"model": "unavailable"` confirms load failure; see logs: `docker logs medical_api \| grep "Model load failed"`. For transient inference errors: `docker logs medical_api \| grep "Inference failed"` |
| `docker build` fails at model download step | No internet access during build | Build requires internet access once to fetch `facebook/bart-large-mnli` (~1.6 GB) |
| `db` connection refused | Postgres not ready | `docker ps` → wait for `(healthy)` on `medical_db` |
| 422 on valid-looking input | `symptoms` < 10 chars | Minimum 10 characters required |
| Port 8000 already in use | Another service on port | `lsof -i :8000`, kill it, or change port in compose |
