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

**Fix — Layer 2, explicit error instead of mock data (safety net):** `get_classifier()` wraps the load in `try/except` and returns `None` on failure. `classify_symptoms()` detects `None` and raises a `RuntimeError`, which the API layer catches and returns as an HTTP 503 Service Unavailable. The service stays alive to serve `/health` (which reports `"model": "unavailable"`) and other endpoints, but explicitly refuses to make fake predictions.

```python
def get_classifier():
    global _classifier
    if _classifier is None:
        try:
            _classifier = pipeline("zero-shot-classification", model=MODEL_NAME)
        except Exception as e:
            logger.error(f"Model load failed: {e}")
            _classifier = None
    return _classifier
```

**How to verify:** If the model fails to load, POSTing to `/predict` will return a 503 error instead of a fake prediction.

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

**Problem:** `classify_symptoms` called the HuggingFace pipeline directly with no retry logic. A single transient error (GPU OOM, thread timeout, etc.) immediately raised `RuntimeError`, which the API layer converted to `HTTP 503 Service Unavailable`, crashing the request with no recovery attempt. When the model failed to load (`_classifier = None`) there was also no fallback — the service simply refused all prediction requests.

**Fix — Layer 1, retry with tenacity:** The synchronous inference call is now wrapped in `_run_inference`, decorated with `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)`. Up to 3 attempts are made with exponential backoff before the failure is considered permanent.

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _run_inference(clf, symptoms: str) -> dict:
    return clf(symptoms, CONDITION_LABELS, multi_label=False)
```

**Fix — Layer 2, default fallback response:** When the classifier is `None` or all retries fail, `classify_symptoms` no longer raises. It returns a safe default dict with `is_fallback=True`, `top_condition="unclassifiable"`, `confidence=0.0`, and a Vietnamese advisory message. The `try/except RuntimeError` in `api.py` is removed; `POST /predict` always returns `201`.

**Fix — Layer 3, audit persistence:** Fallback records are stored in PostgreSQL with an `is_fallback BOOLEAN` column so the care team can identify unclassified requests and data analysts can exclude them from model metrics.

**How to verify:** When the model fails to load or inference throws, `POST /predict` returns `201` with `"is_fallback": true` and `"fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ"` instead of `503`.
