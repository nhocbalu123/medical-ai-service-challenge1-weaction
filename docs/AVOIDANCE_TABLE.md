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

**Fix — Layer 2, explicit error instead of mock data (safety net):** `get_classifier()` wraps the load in `try/except` and returns `None` on failure. `classify_symptoms()` detects `None` and, at this stage, raised a `RuntimeError` that the API layer converted to HTTP 503. The service stayed alive to serve `/health` (which reports `"model": "unavailable"`) and other endpoints, but explicitly refused to make fake predictions.

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

> **Note:** The Layer 2 behaviour (503 on model failure) was subsequently improved in **Mistake 6**. `classify_symptoms` no longer raises; it returns a safe fallback dict with `is_fallback=True`. `POST /predict` now always returns `201 Created` — see Mistake 6 for the current verification steps.

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

---

## Mistake 7 — OTel OTLP endpoint path bypassed by explicit `endpoint=` argument

**Problem:** `app/core/telemetry.py` read `OTEL_EXPORTER_OTLP_ENDPOINT` manually via `os.getenv` and passed the raw value as `endpoint=` to `OTLPSpanExporter`. Per the OTel spec, this env var is a *base URL*; the SDK auto-appends the signal-specific path (`/v1/traces`) only when it reads the var itself. Passing an explicit `endpoint=` argument bypasses that logic entirely — the value is used verbatim as the full URL. The built-in default worked only because it already contained `/v1/traces`; any user setting the standard base URL (e.g. `http://collector:4318`) would silently send traces to `http://collector:4318` instead of `http://collector:4318/v1/traces`, losing all trace data with no error.

**Fix:** Removed the manual `os.getenv` call and the `endpoint=` argument. Instead, `os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")` establishes the default base URL before the exporter is constructed, and `OTLPSpanExporter()` is called with no arguments so the SDK reads the env var natively and appends the correct path.

```python
# before — bypasses SDK path-appending
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318/v1/traces")
BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))

# after — SDK reads env var and appends /v1/traces per spec
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")
BatchSpanProcessor(OTLPSpanExporter())
```

Updated `.env.example`, `.env`, and `README.md` to document `OTEL_EXPORTER_OTLP_ENDPOINT` as a base URL.

---

## Mistake 8 — Narrow exception catch lets DB connection failures escape as 500

**Problem:** The `except asyncpg.PostgresError` handler in `app/routers/api.py` only covered server-acknowledged PostgreSQL errors (constraint violations, syntax errors, etc.). When the database is actually *unreachable*, asyncpg raises `asyncpg.InterfaceError` (pool/connection-management errors) or a low-level `OSError` / `ConnectionRefusedError`. Neither inherits from `PostgresError`, so these connection-level exceptions bypassed the handler entirely, producing an unstructured `500 Internal Server Error` instead of the documented `503 Service Unavailable` with a human-readable message.

**Fix:** Broadened the except clause to cover all three error families:

```python
# before — misses connection-level failures
except asyncpg.PostgresError as exc:

# after — catches server errors, pool/connection errors, and OS-level network errors
except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
```

**How to verify:** Stop the `db` container while the API is running (`docker stop medical_db`) and call `POST /predict`. The response should be `503` with `{"detail": "Database unavailable; the prediction could not be saved. Please retry later."}` and a `db_error_on_predict` log line at `ERROR` level.
