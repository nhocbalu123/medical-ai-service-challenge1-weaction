# AVOIDANCE_TABLE.md — Proof of Avoiding Common Mistakes

> Tôi đã tránh được ≥ 6/8 lỗi thường gặp khi đóng gói AI service với FastAPI + Docker.

---

## Lỗi #1 — Base Image bloat

**Lỗi phổ biến:** Dùng `python:3.11` (full image ~1 GB) cho production.

**Cách tôi xử lý:** Sử dụng `python:3.11-slim` với **multi-stage build** — stage `builder` cài dependencies, stage `runtime` chỉ copy thư viện đã cài. Image cuối cùng nhỏ hơn đáng kể.

```dockerfile
FROM python:3.11-slim AS builder
# install deps with --prefix=/install
FROM python:3.11-slim AS runtime
COPY --from=builder /install /usr/local
```

> 📸 Xem `utils/6-docker-images.png` để kiểm tra image size.

---

## Lỗi #2 — Hardcode secrets

**Lỗi phổ biến:** Viết thẳng password/DB URL vào source code hoặc Dockerfile.

**Cách tôi xử lý:** Toàn bộ secret đọc từ `os.getenv()`. `docker-compose.yml` inject qua biến môi trường với giá trị mặc định an toàn.

```python
# app/services/core.py
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/medicaldb")
```

```yaml
# docker-compose.yml
environment:
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
```

---

## Lỗi #3 — No Pydantic validation

**Lỗi phổ biến:** Nhận input dạng `dict` thô, không validate, dẫn đến lỗi không rõ ràng.

**Cách tôi xử lý:** Dùng Pydantic `BaseModel` với ràng buộc rõ ràng: `min_length`, `max_length`, `ge`, `le`, `field_validator`. FastAPI tự động trả **422 Unprocessable Entity** khi input sai.

```python
class SymptomRequest(BaseModel):
    patient_id: str = Field(..., min_length=1, max_length=64)
    symptoms: str = Field(..., min_length=10, max_length=1000)
    age: Optional[int] = Field(None, ge=0, le=150)
```

---

## Lỗi #4 — Monolith main.py

**Lỗi phổ biến:** Nhét toàn bộ endpoints, DB logic, model logic vào một file `main.py` duy nhất.

**Cách tôi xử lý:** Tổ chức theo **router/service pattern**:
- `app/routers/api.py` — chỉ định nghĩa endpoints
- `app/services/core.py` — business logic (model inference + DB)
- `app/models/schemas.py` — Pydantic models

```
app/
├── main.py          ← chỉ include router
├── routers/api.py   ← HTTP endpoints
├── models/schemas.py← request/response types
└── services/core.py ← business logic
```

---

## Lỗi #5 — DB connection không có healthcheck

**Lỗi phổ biến:** API khởi động trước khi DB sẵn sàng → crash ngay lập tức.

**Cách tôi xử lý:** `docker-compose.yml` dùng `depends_on` với `condition: service_healthy`, kết hợp `healthcheck` trên `db` service:

```yaml
db:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U postgres -d medicaldb"]
    interval: 10s
    retries: 5
    start_period: 10s

api:
  depends_on:
    db:
      condition: service_healthy
```

---

## Lỗi #6 — Chạy container với root user

**Lỗi phổ biến:** Container chạy với user `root` — rủi ro bảo mật.

**Cách tôi xử lý:** Tạo `appuser` và chuyển sang non-root trước khi chạy:

```dockerfile
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser
```

---

## Lỗi #7 — Không có HEALTHCHECK trong Dockerfile

**Lỗi phổ biến:** Container báo `running` nhưng service bên trong đã chết.

**Cách tôi xử lý:** Thêm `HEALTHCHECK` directive trực tiếp trong Dockerfile:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
```

---

## Lỗi #8 — Graceful degradation khi model không load được

**Lỗi phổ biến:** Nếu model HuggingFace chưa download xong hoặc lỗi → API crash hoàn toàn.

**Cách tôi xử lý:** `get_classifier()` dùng try/except, trả `None` nếu lỗi. `classify_symptoms()` phát hiện `clf is None` và chuyển sang **mock mode** (random prediction với cờ `model_version: "x.x.x-mock"`).

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

---

_Tất cả 8/8 lỗi đã được xử lý. Xem source code trong `app/` và `docker/` để kiểm chứng._
