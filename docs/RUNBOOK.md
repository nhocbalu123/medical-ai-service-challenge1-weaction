# RUNBOOK.md — Operations Guide & Mistake Avoidance Log

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

## 3. Mistakes Avoided — Detailed Log

### Mistake 1 — Fat base image
- **Problem:** `python:3.11` is ~1 GB. Production images should be lean.
- **Fix:** `python:3.11-slim` as both builder and runtime. Multi-stage build copies only compiled packages.
- **Result:** Base image ~350–450 MB (vs ~1.3 GB). HuggingFace model weights (~1.6 GB) are pre-downloaded during `docker build` and baked into the final image, so the container never needs internet access at runtime.

### Mistake 2 — Hardcoded credentials
- **Problem:** `DATABASE_URL = "postgresql://postgres:secret123@localhost/db"` in code.
- **Fix:** `os.getenv("DATABASE_URL", "...")` with fallback. Secret injected via `docker-compose.yml` environment block using shell variable `${POSTGRES_PASSWORD:-postgres}`.
- **Principle:** Never commit secrets. Use `.env` file (git-ignored) for local overrides in production.

### Mistake 3 — No input validation
- **Problem:** `request.json()` raw dict → unpredictable errors deep in business logic.
- **Fix:** Pydantic `BaseModel` with `Field(min_length=..., max_length=..., ge=..., le=...)`. Custom `field_validator` for blank check. FastAPI automatically serializes `ValidationError` → HTTP 422 with field-level error messages.

### Mistake 4 — Monolith main.py
- **Problem:** 300-line `main.py` with DB queries, model code, and routes mixed together.
- **Fix:** Three-layer architecture: Router (HTTP) → Service (logic) → Model/Schema (types). Each layer is independently testable.

### Mistake 5 — Race condition on startup
- **Problem:** API container starts and immediately tries to connect to DB that isn't ready yet.
- **Fix:** `depends_on: db: condition: service_healthy` in `docker-compose.yml` + `pg_isready` healthcheck on db service. API only starts after DB passes health check.

### Mistake 6 — Running as root
- **Problem:** Container process running as root means any RCE vulnerability has full host access.
- **Fix:** Create dedicated `appuser` in Dockerfile and `USER appuser` before CMD.

### Mistake 7 — No container health signal
- **Problem:** `docker ps` shows "Up" even if uvicorn crashed inside.
- **Fix:** `HEALTHCHECK` in Dockerfile calls `/health` endpoint every 30s. Container shows `(healthy)` or `(unhealthy)` in `docker ps`.

### Mistake 8 — Hard crash when model fails
- **Problem:** If HuggingFace model download times out or fails, whole service is unavailable. Additionally, the same symptoms returned different results on every request because the service was silently running in random mock mode.
- **Primary fix:** Model weights (`facebook/bart-large-mnli`) are pre-downloaded during `docker build` via an `ARG`/`ENV HF_HOME` pattern and stored at `/hf-cache` inside the image. The runtime stage copies `/hf-cache` and sets `ENV HF_HOME=/hf-cache`, so the model loads from disk with no network call needed.
- **Safety-net fix:** `get_classifier()` still wraps the load in `try/except` and returns `None` on failure. `classify_symptoms()` detects `None` and falls back to mock random predictions, flagging results with `model_version: "x.x.x-mock"`. Service stays alive; `/health` reports `"model": "mock-mode"`. If you see `-mock` in responses, the model failed to load — check container logs for the error.

---

## 4. DB Schema

```sql
CREATE TABLE predictions (
    id            SERIAL PRIMARY KEY,
    patient_id    VARCHAR(64) NOT NULL,
    symptoms      TEXT NOT NULL,
    top_condition VARCHAR(128),
    confidence    FLOAT,
    all_predictions JSONB,
    model_version VARCHAR(32),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `api` container unhealthy | Service not ready yet | Wait 30–60 s, check `docker logs medical_api` |
| Classifier returns different result every call | Model failed to load; running in mock mode | Check `model_version` field — if it ends in `-mock`, see logs: `docker logs medical_api \| grep "Model load failed"` |
| `docker build` fails at model download step | No internet access during build | Build requires internet access once to fetch `facebook/bart-large-mnli` (~1.6 GB) |
| `db` connection refused | Postgres not ready | `docker ps` → wait for `(healthy)` on `medical_db` |
| 422 on valid-looking input | `symptoms` < 10 chars | Minimum 10 characters required |
| Port 8000 already in use | Another service on port | `lsof -i :8000`, kill it, or change port in compose |
