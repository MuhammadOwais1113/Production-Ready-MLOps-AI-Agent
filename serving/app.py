"""Containerised FastAPI inference service deployed on Kubernetes.

/health  -> liveness  (process is alive)
/ready   -> readiness (model loaded and usable)  - returns 503 when the model cannot be loaded
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("inference")
STATE: dict = {"model": None, "scaler": None, "error": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    model_path = Path(os.getenv("MODEL_PATH", "/models/model.joblib"))
    scaler_path = Path(os.getenv("SCALER_PATH", str(model_path.with_name("scaler.joblib"))))
    try:
        STATE["model"], STATE["scaler"] = joblib.load(model_path), joblib.load(scaler_path)
        log.info("model loaded from %s (version %s)", model_path, os.getenv("MODEL_VERSION", "unknown"))
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        log.error("MODEL LOAD FAILED: %s", STATE["error"])
    yield


app = FastAPI(title="Inference API", lifespan=lifespan)


class PredictRequest(BaseModel):
    features: list[float] = Field(min_length=8, max_length=8)


@app.get("/health")
def health() -> dict:
    return {"status": "alive"}


@app.get("/ready")
def ready() -> dict:
    if STATE["model"] is None:
        log.error("readiness failed: %s", STATE["error"])
        raise HTTPException(status_code=503, detail=f"model not loaded: {STATE['error']}")
    return {"status": "ready"}


@app.get("/version")
def version() -> dict:
    return {"app_version": os.getenv("APP_VERSION", "dev"), "model_version": os.getenv("MODEL_VERSION", "unknown")}


@app.post("/predict")
def predict(req: PredictRequest) -> dict:
    if STATE["model"] is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    x = STATE["scaler"].transform(np.array([req.features]))
    return {"prediction": int(STATE["model"].predict(x)[0]), "model_version": os.getenv("MODEL_VERSION", "unknown")}
