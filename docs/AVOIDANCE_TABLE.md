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
