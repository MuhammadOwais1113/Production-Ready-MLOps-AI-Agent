from __future__ import annotations

import time

import pytest

from agent.tools.base import ToolRegistry
from agent.tools.mlflow_tools import MlflowTools
from tests.fakes import FakeMlflow, mlflow_run


def reg(client) -> ToolRegistry:
    r = ToolRegistry(5)
    r.register(MlflowTools(lambda: client, "exp", "classifier", gate_f1=0.85, max_age_days=30).tools())
    return r


def test_best_run_selected_by_metric_and_not_blindly_ready():
    runs = [mlflow_run("a" * 32, 1, 0.90, gate="passed"), mlflow_run("b" * 32, 2, 0.95, gate="failed", md5=None), mlflow_run("c" * 32, 3, 0.80)]
    d = reg(FakeMlflow(runs)).execute("find_mlflow_best_run" if False else "find_best_mlflow_run", {"metric": "f1"}).data
    assert d["best"]["run_id"] == "b" * 32  # highest f1
    assert "NOT demonstrated production-ready" in d["readiness"]["verdict"]
    failed = {c["check"] for c in d["readiness"]["checks"] if not c["ok"]}
    assert {"quality_gate_passed", "dataset_provenance_recorded", "registered_in_model_registry"} <= failed
    assert d["runner_up_margin"] == pytest.approx(0.05)


def test_minimise_metric_prefix():
    runs = [mlflow_run("a" * 32, 1, 0.9), mlflow_run("b" * 32, 2, 0.5)]
    assert reg(FakeMlflow(runs)).execute("find_best_mlflow_run", {"metric": "-f1"}).data["best"]["run_id"] == "b" * 32


def test_compare_last_n_runs_recommends_candidate_and_lists_differing_params():
    runs = [mlflow_run(str(i) * 32, i, 0.7 + i / 20, params={"n_estimators": str(i * 50)}) for i in range(1, 6)]
    d = reg(FakeMlflow(runs)).execute("compare_mlflow_runs", {"last_n": 3}).data
    assert len(d["runs"]) == 3 and d["recommended"] == d["ranking"][0] and d["params_that_differ"] == ["n_estimators"]
    assert "caveat" in d


def test_stale_model_flagged_by_freshness_check():
    d = reg(FakeMlflow([mlflow_run("a" * 32, 1, 0.9, age_days=45)])).execute("find_best_mlflow_run", {}).data
    assert any(c["check"].startswith("fresh") and not c["ok"] for c in d["readiness"]["checks"])


def test_run_details_include_artifacts_and_registry_empty():
    r = reg(FakeMlflow([mlflow_run("a" * 32, 1, 0.9)]))
    assert r.execute("get_mlflow_run_details", {"run_id": "a" * 32}).data["artifacts"][0]["path"] == "model.joblib"
    assert r.execute("get_model_registry_info", {}).data["registered"] is False


def test_unreachable_server_is_unavailable_not_exception():
    r = reg(FakeMlflow(fail=ConnectionError("connection refused"))).execute("list_mlflow_runs", {})
    assert r.status == "unavailable" and "refused" in r.error


def test_real_mlflow_sqlite_roundtrip(tmp_path):
    """Integration against a real MLflow SQLite backend (no mocks)."""
    pytest.importorskip("sqlalchemy")
    from mlflow.tracking import MlflowClient
    uri = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    c = MlflowClient(uri)
    eid = c.create_experiment("exp")
    for i, f1 in enumerate((0.7, 0.9, 0.8), 1):
        run = c.create_run(eid, tags={"run_number": str(i), "quality_gate": "passed", "dvc.md5": "x"})
        c.log_metric(run.info.run_id, "f1", f1)
        c.log_param(run.info.run_id, "n", i)
        c.set_terminated(run.info.run_id)
        time.sleep(0.01)
    r = ToolRegistry(20)
    r.register(MlflowTools(lambda: MlflowClient(uri), "exp", "classifier").tools())
    best = r.execute("find_best_mlflow_run", {}).data
    assert best["best"]["metrics"]["f1"] == 0.9 and best["runs_considered"] == 3
    assert len(r.execute("list_mlflow_experiments", {}).data["experiments"]) >= 1
