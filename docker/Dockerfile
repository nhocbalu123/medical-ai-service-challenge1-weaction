# ── Stage 1: builder ────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip     && pip install --prefix=/install --no-cache-dir -r requirements.txt

# Pre-download model weights into a fixed path so appuser can read them at runtime
ARG MODEL_NAME=facebook/bart-large-mnli
ENV HF_HOME=/hf-cache
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    python -c "from transformers import pipeline; pipeline('zero-shot-classification', model='${MODEL_NAME}')"


# ── Stage 2: runtime ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="your-name"
LABEL description="Medical Symptom Classifier API"

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /install /usr/local
# Copy pre-downloaded model weights and make them readable by appuser
COPY --from=builder /hf-cache /hf-cache
RUN chmod -R a+rX /hf-cache

# Copy application source
COPY app/ ./app/

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

ENV HF_HOME=/hf-cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
