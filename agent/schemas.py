"""Structured inputs/outputs shared by tools, orchestrator and API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolStatus = Literal["ok", "error", "timeout", "unavailable", "invalid", "denied", "needs_confirmation", "unknown_tool"]


class ToolResult(BaseModel):
    tool: str
    status: ToolStatus
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    request_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    confirm: bool = Field(default=False, description="Explicit user confirmation for state-changing operations")
    request_id: str | None = None


class Evidence(BaseModel):
    tool: str
    status: str
    summary: str


class ChatResponse(BaseModel):
    request_id: str
    mode: str
    answer: str
    known_facts: list[str] = []
    uncertainties: list[str] = []
    recommended_actions: list[str] = []
    evidence: list[Evidence] = []
    tool_calls: list[ToolResult] = []


# ---- tool argument models -------------------------------------------------
class NoArgs(BaseModel):
    pass


class DatasetArgs(BaseModel):
    dataset: str | None = Field(default=None, description="Path of the .dvc file, e.g. data/dataset.csv.dvc")


class CompareDatasetArgs(BaseModel):
    rev_a: str = Field(description="Git revision / tag of the first dataset version, e.g. v1.0")
    rev_b: str = Field(default="HEAD", description="Git revision / tag of the second dataset version")
    dataset: str | None = None


class ReproduceArgs(BaseModel):
    rev: str = Field(description="Git revision / tag to reproduce")
    dataset: str | None = None


class CheckoutArgs(BaseModel):
    rev: str
    dataset: str | None = None


class RunRefArgs(BaseModel):
    run_id: str | None = Field(default=None, description="MLflow run id (defaults to newest run)")


class ExperimentRunRef(BaseModel):
    run_id: str | None = Field(default=None, description="MLflow run id")
    run_number: int | None = Field(default=None, ge=1, description="Sequential training run number (the 'experiment 25' in a user request)")


class ListRunsArgs(BaseModel):
    experiment_name: str | None = None
    limit: int = Field(default=5, ge=1, le=50)


class BestRunArgs(BaseModel):
    metric: str = Field(default="f1", description="Metric name to maximise (prefix '-' to minimise)")
    experiment_name: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class CompareRunsArgs(BaseModel):
    run_ids: list[str] | None = None
    last_n: int | None = Field(default=None, ge=2, le=20)
    metric: str = "f1"
    experiment_name: str | None = None


class CompareExperimentsArgs(BaseModel):
    experiment_names: list[str] = Field(min_length=2)
    metric: str = "f1"


class RegistryArgs(BaseModel):
    model_name: str | None = None


class DagArgs(BaseModel):
    dag_id: str = "training_pipeline"


class DagRunArgs(BaseModel):
    dag_id: str = "training_pipeline"
    dag_run_id: str | None = Field(default=None, description="Defaults to the latest run")
    limit: int = Field(default=5, ge=1, le=50)


class TriggerArgs(BaseModel):
    dag_id: str = "training_pipeline"
    conf: dict[str, Any] = Field(default_factory=dict)


class TaskLogArgs(BaseModel):
    dag_id: str = "training_pipeline"
    dag_run_id: str | None = None
    task_id: str | None = Field(default=None, description="Defaults to the first failed task")


class K8sNsArgs(BaseModel):
    namespace: str | None = None


class PodArgs(BaseModel):
    namespace: str | None = None
    pod_name: str | None = Field(default=None, description="Defaults to the least healthy pod")
    tail_lines: int = Field(default=50, ge=1, le=500)


class RestartArgs(BaseModel):
    namespace: str | None = None
    deployment: str | None = None


class ScaleArgs(BaseModel):
    namespace: str | None = None
    deployment: str | None = None
    replicas: int = Field(ge=0)
