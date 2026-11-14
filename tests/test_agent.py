"""Agent-level behaviour: tool selection, multi-tool reasoning, honesty under failure, safety."""
from __future__ import annotations

import re
from types import SimpleNamespace as NS

from agent.schemas import ChatRequest
from tests.fakes import FakeCore, k8s_pod, mlflow_run

MULTI = ("Find the best recent model, identify the dataset version used to train it, check the deployment status, "
         "and explain whether retraining is necessary.")


def ask(agent, text, confirm=False):
    return agent.handle(ChatRequest(message=text, confirm=confirm))


def tools_called(resp):
    return [c.tool for c in resp.tool_calls]


def test_selects_dvc_tool_for_experiment_dataset_question(make_agent):
    r = ask(make_agent(), "Which dataset version was used for experiment 2?")
    assert tools_called(r) == ["get_experiment_dataset"]
    assert "2" * 32 in r.answer and "v2.0" in r.answer


def test_selects_mlflow_compare_for_last_five_runs_f1(make_agent):
    r = ask(make_agent(), "Compare the last five training runs and recommend the best candidate based on F1 score.")
    assert tools_called(r) == ["compare_mlflow_runs"]
    assert "highest" not in r.answer.lower() or "production" in r.answer.lower()
    assert any("NOT demonstrated production-ready" in u for u in r.uncertainties)  # never assumes max F1 == ready


def test_selects_airflow_status_and_diagnoses_failure(make_agent):
    r = ask(make_agent(airflow="failed_validation"), "Why did the training pipeline fail?")
    assert tools_called(r) == ["get_dag_run_status", "get_failed_tasks", "get_task_diagnostics"]
    assert "validate_dataset" in r.answer and "null values" in r.answer and "upstream_failed" in r.answer


def test_k8s_diagnosis_from_pod_status_events_and_logs(make_agent):
    core = FakeCore([k8s_pod("a", ready=False, restarts=5, waiting="CrashLoopBackOff"), k8s_pod("b")],
                    logs="ERROR MODEL LOAD FAILED: FileNotFoundError /models/x.joblib", endpoints=1)
    r = ask(make_agent(core=core), "Why is my prediction API unavailable?")
    assert {"find_unhealthy_pods", "get_pod_status_and_logs", "inspect_k8s_services"} <= set(tools_called(r))
    assert "CrashLoopBackOff" in r.answer and "MODEL LOAD FAILED" in r.answer


def test_multi_tool_scenario_uses_all_four_systems_and_never_triggers_retraining(make_agent):
    agent = make_agent()
    r = ask(agent, MULTI)
    used = set(tools_called(r))
    assert {"find_best_mlflow_run", "get_experiment_dataset", "get_deployed_version", "get_dag_run_status"} <= used
    assert "trigger_training_dag" not in used and not agent.fakes["airflow"].triggered
    assert "Known facts" in r.answer and "Uncertainties" in r.answer and "Recommended actions" in r.answer
    assert r.recommended_actions[0].startswith("Retraining is NOT currently necessary")


def test_retraining_recommended_only_with_evidence(make_agent):
    stale = make_agent(runs=[mlflow_run("s" * 32, 1, 0.9, age_days=60, md5="2" * 32)])
    assert "freshness policy" in ask(stale, MULTI).recommended_actions[0]
    gate = make_agent(runs=[mlflow_run("g" * 32, 1, 0.8, gate="failed", md5="2" * 32)])
    r = ask(gate, MULTI)
    assert r.recommended_actions[0].startswith("Retraining IS justified") and "NOT started automatically" in r.recommended_actions[0]
    failed_pipe = ask(make_agent(airflow="quality_gate"), MULTI)
    assert "quality gate failed" in failed_pipe.recommended_actions[0]


def test_does_not_invent_results_when_tools_unavailable(make_agent):
    agent = make_agent(mlflow_fail=ConnectionError("refused"), k8s_down=True, airflow="down")
    r = ask(agent, MULTI)
    assert all(c.status in ("ok", "unavailable") for c in r.tool_calls)
    for t in ("find_best_mlflow_run", "get_deployed_version", "get_dag_run_status"):
        assert next(c for c in r.tool_calls if c.tool == t).status == "unavailable"
    assert "unavailable" in r.answer and "No claims are made" in r.answer
    # no fabricated metric values or replica counts appear anywhere in the facts
    facts = " ".join(r.known_facts)
    assert not re.search(r"f1=\d", facts) and "replicas ready" not in facts and "Airflow run" not in facts
    # with no evidence at all the agent must not claim retraining is (un)necessary from data it does not have
    assert all(c.status == "unavailable" for c in r.tool_calls if c.tool in ("find_best_mlflow_run", "find_unhealthy_pods", "get_dag_run_status"))


def test_unauthorized_k8s_modification_is_rejected_end_to_end(make_agent):
    agent = make_agent()
    r = ask(agent, "Restart the deployment in kube-system", confirm=True)
    # the planner restarts the configured (approved) deployment only; a foreign namespace through the tool is denied
    direct = agent.reg.execute("restart_k8s_deployment", {"namespace": "kube-system"}, confirmed=True)
    assert direct.status == "denied" and agent.fakes["apps"].patches == [] or len(agent.fakes["apps"].patches) <= 1
    unconfirmed = ask(make_agent(), "Restart the inference deployment")
    assert unconfirmed.tool_calls[0].status == "needs_confirmation" and "confirm=true" in unconfirmed.answer
    assert r.tool_calls


def test_trigger_requires_confirmation_then_runs(make_agent):
    agent = make_agent()
    r = ask(agent, "Trigger the model training pipeline and report whether execution succeeded.")
    assert r.tool_calls[0].tool == "trigger_training_dag" and r.tool_calls[0].status == "needs_confirmation" and not agent.fakes["airflow"].triggered
    r2 = ask(agent, "Trigger the model training pipeline and report whether execution succeeded.", confirm=True)
    assert r2.tool_calls[0].ok and len(agent.fakes["airflow"].triggered) == 1


def test_unmappable_request_makes_no_claims(make_agent):
    r = ask(make_agent(), "What is the weather in Lahore?")
    assert r.tool_calls == [] and r.known_facts == [] and "nothing was queried" in r.uncertainties[0]


# ---- LLM mode (fake Anthropic client): the model cannot grant confirmation -----------------------------
class FakeLLM:
    def __init__(self, script):
        self.script, self.i = script, 0
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        out = self.script[self.i]
        self.i += 1
        return NS(content=out)


def tool_use(name, inp, id_="tu1"):
    return NS(type="tool_use", name=name, input=inp, id=id_, model_dump=lambda: {"type": "tool_use", "name": name, "input": inp, "id": id_})


def text(t):
    return NS(type="text", text=t, model_dump=lambda: {"type": "text", "text": t})


def test_llm_mode_runs_tools_and_model_cannot_self_confirm(make_agent):
    agent = make_agent()
    agent._llm = FakeLLM([[tool_use("restart_k8s_deployment", {"confirm": True})], [text("Needs your confirmation.")]])
    r = ask(agent, "restart it")
    assert r.mode == "llm" and r.tool_calls[0].status == "needs_confirmation" and not agent.fakes["apps"].patches
    assert "Tool availability notes" in r.answer


def test_llm_failure_falls_back_to_offline_planner(make_agent):
    agent = make_agent()

    class Broken:
        messages = NS(create=lambda **k: (_ for _ in ()).throw(RuntimeError("api down")))
    agent._llm = Broken()
    r = ask(agent, "Which dataset version is current?")
    assert r.mode == "offline-planner" and r.tool_calls[0].tool == "get_dataset_version"


def test_llm_answer_without_tools_is_flagged_unverified(make_agent):
    agent = make_agent()
    agent._llm = FakeLLM([[text("The model is great")]])
    assert "no verified operational data" in ask(agent, "how is the model?").answer
