"""Test doubles for Kubernetes, Airflow, MLflow and DVC so CI never needs real infrastructure."""
from __future__ import annotations

import time
from types import SimpleNamespace as NS
from typing import Any

import requests

# ----------------------------------------------------------------- Kubernetes


def k8s_pod(name: str, phase: str = "Running", ready: bool = True, restarts: int = 0, waiting: str | None = None,
            last_term: str | None = None, image: str = "mlops-inference:1.0.0") -> NS:
    state = NS(waiting=NS(reason=waiting, message="back-off") if waiting else None, terminated=None)
    last = NS(terminated=NS(reason=last_term, exit_code=137 if last_term == "OOMKilled" else 1)) if last_term else None
    cs = NS(name="inference-api", ready=ready, restart_count=restarts, image=image, state=state, last_state=last)
    return NS(metadata=NS(name=name, labels={"app": "inference-api"}), status=NS(phase=phase, container_statuses=[cs]),
              spec=NS(node_name="node-1"))


class FakeCore:
    def __init__(self, pods: list[NS], logs: str = "INFO started\n", events: list[NS] | None = None,
                 endpoints: int = 2, fail: Exception | None = None) -> None:
        self.pods, self.logs, self.events, self.endpoints, self.fail = pods, logs, events or [], endpoints, fail
        self.log_calls: list[dict[str, Any]] = []

    def _chk(self) -> None:
        if self.fail:
            raise self.fail

    def list_namespaced_pod(self, ns: str, label_selector: str | None = None) -> NS:
        self._chk()
        return NS(items=self.pods)

    def read_namespaced_pod_log(self, name: str, ns: str, **kw: Any) -> str:
        self.log_calls.append({"name": name, **kw})
        return self.logs

    def list_namespaced_event(self, ns: str, field_selector: str | None = None) -> NS:
        return NS(items=self.events)

    def list_namespaced_service(self, ns: str) -> NS:
        self._chk()
        return NS(items=[NS(metadata=NS(name="inference-api"), spec=NS(type="ClusterIP", cluster_ip="10.0.0.5",
                                                                     selector={"app": "inference-api"},
                                                                     ports=[NS(port=80, target_port=8000)]))])

    def read_namespaced_endpoints(self, name: str, ns: str) -> NS:
        return NS(subsets=[NS(addresses=[object()] * self.endpoints)] if self.endpoints else [])


class FakeApps:
    def __init__(self, image: str = "mlops-inference:1.0.0", desired: int = 2, ready: int = 2) -> None:
        self.image, self.desired, self.ready = image, desired, ready
        self.patches: list[tuple[str, str, Any]] = []

    def list_namespaced_deployment(self, ns: str, label_selector: str | None = None) -> NS:
        d = NS(metadata=NS(name="inference-api", generation=3),
               spec=NS(replicas=self.desired, template=NS(metadata=NS(labels={"app.kubernetes.io/version": "1.0.0"}),
                                                          spec=NS(containers=[NS(image=self.image)]))),
               status=NS(ready_replicas=self.ready, available_replicas=self.ready, updated_replicas=self.ready, observed_generation=3))
        return NS(items=[d])

    def patch_namespaced_deployment(self, name: str, ns: str, body: Any) -> None:
        self.patches.append(("patch", f"{ns}/{name}", body))

    def patch_namespaced_deployment_scale(self, name: str, ns: str, body: Any) -> None:
        self.patches.append(("scale", f"{ns}/{name}", body))


def k8s_factory(core: FakeCore, apps: FakeApps):
    return lambda: (core, apps)


# -------------------------------------------------------------------- Airflow
class FakeResp:
    def __init__(self, status: int = 200, body: Any = None, text: str = "") -> None:
        self.status_code, self._body, self.text = status, body, text or str(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError
        return self._body


class FakeAirflowSession:
    """Routes Airflow 3 REST calls to canned JSON. scenario: 'ok' | 'failed_validation' | 'quality_gate' | 'down'."""

    def __init__(self, scenario: str = "ok") -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, str]] = []
        self.token_requests = 0
        self.triggered: list[dict[str, Any]] = []

    def post(self, url: str, json: dict | None = None, timeout: float = 0, **_: Any) -> FakeResp:
        if self.scenario == "down":
            raise requests.ConnectionError("refused")
        self.token_requests += 1
        return FakeResp(201, {"access_token": "eyJhbGciOiJIUzI1NiJ9.payload.sig"})

    def request(self, method: str, url: str, headers: dict | None = None, timeout: float = 0, params: dict | None = None,
                json: dict | None = None, **_: Any) -> FakeResp:
        if self.scenario == "down":
            raise requests.ConnectionError("refused")
        assert headers and headers["Authorization"].startswith("Bearer ")
        path = url.split("/api/v2", 1)[1]
        self.calls.append((method, path))
        failed_task = {"failed_validation": "validate_dataset", "quality_gate": "quality_gate"}.get(self.scenario)
        state = "failed" if failed_task else "success"
        if method == "POST" and path.endswith("/dagRuns"):
            self.triggered.append(json or {})
            return FakeResp(200, {"dag_run_id": "manual__new", "state": "queued", "run_type": "manual"})
        if path == "/dags":
            return FakeResp(200, {"dags": [{"dag_id": "training_pipeline", "is_paused": False}, {"dag_id": "other", "is_paused": True}], "total_entries": 2})
        if path.endswith("/taskInstances"):
            order = ["validate_dataset", "preprocess_data", "train_model", "evaluate_and_log_mlflow", "quality_gate", "promote_model"]
            tis = []
            hit = False
            for t in order:
                if failed_task and t == failed_task:
                    tis.append({"task_id": t, "state": "failed", "try_number": 3, "max_tries": 2})
                    hit = True
                elif hit:
                    tis.append({"task_id": t, "state": "upstream_failed", "try_number": 0, "max_tries": 2})
                else:
                    tis.append({"task_id": t, "state": "success", "try_number": 1, "max_tries": 2})
            return FakeResp(200, {"task_instances": tis})
        if "/logs/" in path:
            msg = ("ERROR dataset validation failed: null values found: {'f3': 375}" if self.scenario == "failed_validation"
                   else "ERROR quality gate failed: f1=0.6189 < threshold 0.85. Promotion blocked.")
            return FakeResp(200, {"content": ["INFO starting", msg, "airflow.exceptions.AirflowFailException"]})
        if "/dagRuns/" in path:
            return FakeResp(200, {"dag_run_id": "scheduled__1", "state": state, "run_type": "manual"})
        if path.endswith("/dagRuns"):
            return FakeResp(200, {"dag_runs": [{"dag_run_id": "manual__1", "state": state, "run_type": "manual"}]})
        return FakeResp(404, {})


# ----------------------------------------------------------------- MLflow/DVC
def mlflow_run(run_id: str, number: int, f1: float, gate: str | None = "passed", md5: str | None = "abc123", age_days: float = 1,
               params: dict | None = None) -> NS:
    tags = {"run_number": str(number), "mlflow.runName": f"rf-{number}", "dvc.file": "data/dataset.csv.dvc"}
    if gate:
        tags["quality_gate"] = gate
    if md5:
        tags.update({"dvc.md5": md5, "dvc.version": "v2.0", "git.commit": "a" * 40})
    return NS(info=NS(run_id=run_id, status="FINISHED", start_time=int(time.time() * 1000 - age_days * 86_400_000)),
              data=NS(metrics={"f1": f1, "accuracy": f1 - 0.01}, params=params or {"n_estimators": "100"}, tags=tags))


class FakeMlflow:
    def __init__(self, runs: list[NS] | None = None, fail: Exception | None = None, registered: list[NS] | None = None) -> None:
        self.runs, self.fail, self.registered = runs or [], fail, registered or []

    def _chk(self) -> None:
        if self.fail:
            raise self.fail

    def get_experiment_by_name(self, name: str) -> NS | None:
        self._chk()
        return NS(experiment_id="1", name=name)

    def search_experiments(self) -> list[NS]:
        self._chk()
        return [NS(experiment_id="1", name="exp", lifecycle_stage="active")]

    def search_runs(self, ids: list[str], filter_string: str = "", max_results: int = 100, order_by: list[str] | None = None) -> list[NS]:
        self._chk()
        runs = sorted(self.runs, key=lambda r: r.info.start_time, reverse=bool(order_by and "DESC" in order_by[0]))
        return runs[:max_results]

    def get_run(self, rid: str) -> NS:
        self._chk()
        return next(r for r in self.runs if r.info.run_id == rid)

    def list_artifacts(self, rid: str) -> list[NS]:
        return [NS(path="model.joblib", is_dir=False, file_size=1234)]

    def search_model_versions(self, q: str) -> list[NS]:
        return self.registered

    def get_registered_model(self, name: str) -> NS:
        if not self.registered:
            raise Exception("RESOURCE_DOES_NOT_EXIST: model")
        return NS(aliases={})
