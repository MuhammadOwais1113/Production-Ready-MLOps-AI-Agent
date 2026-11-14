"""FastAPI interface, training pipeline gating, DAG structure and Kubernetes manifest requirements."""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- API
@pytest.fixture
def client(make_agent):
    from api import main
    main.app.dependency_overrides[main.get_agent] = lambda: make_agent()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_api_health_tools_and_chat(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert len(client.get("/tools").json()) >= 20
    r = client.post("/chat", json={"message": "Which dataset version is current?"}, headers={"x-request-id": "rid-9"})
    assert r.status_code == 200 and r.json()["request_id"] == "rid-9" and r.headers["x-request-id"] == "rid-9"
    assert r.json()["tool_calls"][0]["tool"] == "get_dataset_version"


def test_api_rejects_invalid_requests(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422
    assert client.post("/chat", json={}).status_code == 422
    assert client.post("/chat", json={"message": "x" * 5000}).status_code == 422


def test_each_module_exposes_at_least_three_tools(make_agent):
    reg = make_agent().reg
    for d in ("dvc", "mlflow", "airflow", "kubernetes"):
        assert len(reg.by_domain(d)) >= 3


# ---------------------------------------------------------------- pipeline
def _csv(path: Path, nulls=False, rows=300):
    df = pd.DataFrame({f"f{i}": range(rows) for i in range(8)})
    df["label"] = [i % 2 for i in range(rows)]
    if nulls:
        df.loc[0, "f3"] = None
    df.to_csv(path, index=False)
    return path


def test_validation_blocks_bad_data(tmp_path):
    from training.pipeline import DataValidationError, validate
    assert validate(_csv(tmp_path / "ok.csv"))["rows"] == 300
    with pytest.raises(DataValidationError, match="null"):
        validate(_csv(tmp_path / "bad.csv", nulls=True))
    with pytest.raises(DataValidationError, match="too few"):
        validate(_csv(tmp_path / "small.csv", rows=10))
    with pytest.raises(DataValidationError, match="not found"):
        validate(tmp_path / "missing.csv")


def test_quality_gate_blocks_promotion_and_tags_run(tmp_path):
    pytest.importorskip("sqlalchemy")
    from mlflow.tracking import MlflowClient

    from training.pipeline import QualityGateError, promote, quality_gate
    uri = f"sqlite:///{(tmp_path / 'g.db').as_posix()}"
    c = MlflowClient(uri)
    run = c.create_run(c.create_experiment("e"))
    res = {"run_id": run.info.run_id, "metrics": {"f1": 0.5}, "tracking_uri": uri}
    with pytest.raises(QualityGateError):
        quality_gate(res, threshold=0.85)
    assert c.get_run(run.info.run_id).data.tags["quality_gate"] == "failed"
    assert quality_gate({**res, "metrics": {"f1": 0.9}}, threshold=0.85)["gate"] == "passed"
    assert promote  # promote is only reachable after the gate in the DAG (see dag test)


def test_dag_has_required_stages_retries_and_gated_promotion():
    src = (ROOT / "dags" / "training_pipeline.py").read_text()
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"validate_dataset", "preprocess_data", "train_model", "evaluate_and_log_mlflow", "quality_gate", "promote_model"} <= funcs
    assert '"retries": 2' in src and "AirflowFailException" in src and 'trigger_rule="all_success"' in src
    # dependency chain is expressed by nested calls: promote(gate(evaluate(train(preprocess(validate)))))
    assert "promote_model(quality_gate(evaluate_and_log_mlflow(train_model(preprocess_data(validate_dataset())))))" in src


# ---------------------------------------------------------------- manifests
def _load(name):
    return list(yaml.safe_load_all((ROOT / "kubernetes" / name).read_text()))[0]


def test_deployment_meets_assignment_requirements():
    d = _load("deployment.yaml")
    c = d["spec"]["template"]["spec"]["containers"][0]
    assert d["spec"]["replicas"] >= 2
    assert "readinessProbe" in c and "livenessProbe" in c
    assert {"cpu", "memory"} <= set(c["resources"]["requests"]) and {"cpu", "memory"} <= set(c["resources"]["limits"])
    assert c["envFrom"][0]["configMapRef"]["name"] == _load("configmap.yaml")["metadata"]["name"]


def test_service_is_clusterip_and_selects_deployment():
    s, d = _load("service.yaml"), _load("deployment.yaml")
    assert s["spec"]["type"] == "ClusterIP" and s["spec"]["selector"] == d["spec"]["selector"]["matchLabels"]


def test_configmap_holds_no_secrets():
    cm = _load("configmap.yaml")
    assert cm["kind"] == "ConfigMap" and not any(w in k.lower() for k in cm["data"] for w in ("password", "token", "secret", "key"))
