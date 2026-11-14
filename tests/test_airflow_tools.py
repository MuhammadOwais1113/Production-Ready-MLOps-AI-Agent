from __future__ import annotations

from agent.tools.airflow_tools import AirflowClient, AirflowTools
from agent.tools.base import ToolRegistry
from tests.fakes import FakeAirflowSession


def reg(scenario="ok", pw="pw"):
    s = FakeAirflowSession(scenario)
    r = ToolRegistry(5)
    r.register(AirflowTools(AirflowClient("http://af:8080", "admin", pw, session=s), ["training_pipeline"]).tools())
    return r, s


def test_lists_dags_with_approval_flag_and_uses_jwt_login_once():
    r, s = reg()
    d = r.execute("list_airflow_dags", {}).data
    assert {x["dag_id"]: x["approved_for_trigger"] for x in d["dags"]} == {"training_pipeline": True, "other": False}
    r.execute("list_airflow_dags", {})
    assert s.token_requests == 1  # token is cached


def test_run_status_lists_task_states():
    r, _ = reg()
    d = r.execute("get_dag_run_status", {}).data
    assert d["runs"][0]["state"] == "success" and d["runs"][0]["tasks"]["quality_gate"] == "success"


def test_trigger_requires_confirmation_and_approval():
    r, s = reg()
    assert r.execute("trigger_training_dag", {}).status == "needs_confirmation" and not s.triggered
    assert r.execute("trigger_training_dag", {"conf": {"x": 1}}, confirmed=True).ok and s.triggered == [{"logical_date": None, "conf": {"x": 1}}]
    denied = r.execute("trigger_training_dag", {"dag_id": "other"}, confirmed=True)
    assert denied.status == "denied" and len(s.triggered) == 1


def test_failed_validation_identified_with_root_cause_and_downstream_blocked():
    r, _ = reg("failed_validation")
    f = r.execute("get_failed_tasks", {}).data
    assert f["root_failures"] == ["validate_dataset"]
    assert {t["task_id"] for t in f["failed_tasks"]} >= {"validate_dataset", "promote_model"}
    states = r.execute("get_dag_run_status", {}).data["runs"][0]["tasks"]
    assert states["promote_model"] == "upstream_failed"  # promotion prevented
    diag = r.execute("get_task_diagnostics", {}).data
    assert diag["task_id"] == "validate_dataset" and any("null values" in e for e in diag["error_lines"])


def test_quality_gate_failure_diagnostics():
    r, _ = reg("quality_gate")
    d = r.execute("get_task_diagnostics", {}).data
    assert d["task_id"] == "quality_gate" and any("f1=0.6189" in e for e in d["error_lines"])


def test_airflow_down_is_unavailable():
    r, _ = reg("down")
    res = r.execute("get_dag_run_status", {})
    assert res.status == "unavailable" and "unreachable" in res.error


def test_missing_credentials_is_unavailable_not_crash():
    r, _ = reg(pw="")
    assert r.execute("list_airflow_dags", {}).status == "unavailable"
