from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime


class SymptomRequest(BaseModel):
    patient_id: str = Field(..., min_length=1, max_length=64, description="Unique patient identifier")
    symptoms: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Free-text description of patient symptoms (10–1000 chars)"
    )
    age: Optional[int] = Field(None, ge=0, le=150, description="Patient age (0–150)")
    notes: Optional[str] = Field(None, max_length=500, description="Additional clinical notes")

    @field_validator("symptoms")
    @classmethod
    def symptoms_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("symptoms must not be blank")
        return v.strip()

    model_config = {
        "json_schema_extra": {
            "example": {
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache, fever of 38.5°C, and stiff neck for 3 days",
                "age": 34,
                "notes": "No prior neurological history"
            }
        }
    }


class ConditionScore(BaseModel):
    label: str
    score: float


class PredictionResponse(BaseModel):
    record_id: int
    patient_id: str
    symptoms: str
    top_condition: str
    confidence: float
    all_predictions: list[ConditionScore]
    model_version: str
    created_at: datetime
    is_fallback: bool = False
    fallback_message: Optional[str] = None

    model_config = {"from_attributes": True}


class HealthResponse(BaseModel):
    status: str
    db: str
    model: str
    version: str
