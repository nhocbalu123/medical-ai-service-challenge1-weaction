"""
Shared constants used across services and providers.

Keeping CONDITION_LABELS in one place prevents drift between the HuggingFace
zero-shot classifier and any API-based fallback providers.
"""

CONDITION_LABELS: list[str] = [
    "migraine",
    "bacterial meningitis",
    "common cold",
    "influenza",
    "COVID-19",
    "hypertension",
    "appendicitis",
    "urinary tract infection",
    "anxiety disorder",
    "pneumonia",
]
