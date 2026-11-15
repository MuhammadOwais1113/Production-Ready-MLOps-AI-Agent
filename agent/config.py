"""Central configuration. Everything comes from environment variables; no secrets in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    repo_root: Path = field(default_factory=lambda: Path(os.getenv("REPO_ROOT", Path(__file__).resolve().parents[1])))
    # LLM (optional: without a key the deterministic planner is used)
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", "claude-sonnet-5-5"))
    max_steps: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_STEPS", "10")))
    # DVC
    dvc_data_files: list[str] = field(default_factory=lambda: _csv("DVC_DATA_FILES", "data/dataset.csv.dvc"))
    # MLflow
    mlflow_tracking_uri: str = field(default_factory=lambda: os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    mlflow_experiment: str = field(default_factory=lambda: os.getenv("MLFLOW_EXPERIMENT", "mlops-agent-demo"))
    registered_model: str = field(default_factory=lambda: os.getenv("MLFLOW_MODEL_NAME", "classifier"))
    # Airflow 3
    airflow_url: str = field(default_factory=lambda: os.getenv("AIRFLOW_URL", "http://127.0.0.1:8080"))
    airflow_username: str = field(default_factory=lambda: os.getenv("AIRFLOW_USERNAME", "admin"))
    airflow_password: str = field(default_factory=lambda: os.getenv("AIRFLOW_PASSWORD", ""))
    approved_dags: list[str] = field(default_factory=lambda: _csv("APPROVED_DAGS", "training_pipeline"))
    # Kubernetes
    approved_namespaces: list[str] = field(default_factory=lambda: _csv("APPROVED_NAMESPACES", "mlops-agent"))
    k8s_namespace: str = field(default_factory=lambda: os.getenv("K8S_NAMESPACE", "mlops-agent"))
    k8s_app_label: str = field(default_factory=lambda: os.getenv("K8S_APP_LABEL", "app=inference-api"))
    k8s_deployment: str = field(default_factory=lambda: os.getenv("K8S_DEPLOYMENT", "inference-api"))
    max_replicas: int = field(default_factory=lambda: int(os.getenv("K8S_MAX_REPLICAS", "5")))
    # Policy
    tool_timeout_s: float = field(default_factory=lambda: float(os.getenv("TOOL_TIMEOUT_S", "20")))
    max_model_age_days: int = field(default_factory=lambda: int(os.getenv("MAX_MODEL_AGE_DAYS", "30")))
    quality_gate_f1: float = field(default_factory=lambda: float(os.getenv("QUALITY_GATE_F1", "0.85")))
