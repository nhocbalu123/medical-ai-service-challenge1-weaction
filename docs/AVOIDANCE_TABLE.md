# AVOIDANCE_TABLE.md — Real Issue Resolved

---

## Problem — Classifier returned random results every time

**Problem:** Classifier returned random results every time the same symptoms were posted.

**Root cause:** The model (`facebook/bart-large-mnli`) was not being pre-downloaded during `docker build`. At runtime, when the container tried to download the model from HuggingFace, the download failed (no internet access in the container environment). The service silently fell back to mock mode, which generates random predictions and flags them with `model_version: "x.x.x-mock"`.

**Solution — two-layer fix:**

**Layer 1 — Pre-download model at build time (primary fix):** Model weights are now downloaded during `docker build` via an `ARG`/`ENV` pattern and stored at `/hf-cache` inside the image. The runtime stage copies `/hf-cache` with read-only permissions set for the non-root `appuser`, so predictions use the real model with zero network calls.

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

**Layer 2 — Graceful fallback (safety net):** If the model still fails to load at runtime (edge case), `get_classifier()` catches the error and returns `None`. `classify_symptoms()` detects this and falls back to mock mode, but now the response includes `model_version: "x.x.x-mock"` as a clear diagnostic flag. Service stays alive; `/health` reports `"model": "mock-mode"`.

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

**How to verify the fix works:** After `docker compose build`, run a prediction twice with identical symptoms. If you get the same result both times (and `model_version` is `"1.0.0"` not `"1.0.0-mock"`), the real model is loaded and working correctly.
