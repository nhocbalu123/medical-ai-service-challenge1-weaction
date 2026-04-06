import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import api
from app.services import core


@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.init_db()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, core.get_classifier)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Medical Symptom Classifier API",
    description="""
## 🏥 Medical Symptom Classifier Service

Wraps a fine-tuned HuggingFace text classification model to predict possible medical conditions 
from patient symptom descriptions. All predictions are saved to PostgreSQL for tracking.

> ⚠️ This service is for **demo/educational purposes only** and is NOT a substitute for professional medical advice.
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(api.router)
