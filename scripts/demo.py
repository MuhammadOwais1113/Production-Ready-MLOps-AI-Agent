"""Evidence generator: runs the five demo scenarios + two injected failures through the agent.

REAL backends : DVC (git + dvc CLI, local remote) and MLflow (SQLite store with the runs created by training/train.py)
MOCKED backends: Kubernetes and Airflow, unless AIRFLOW_PASSWORD / a kube context is configured and --live is passed.
Output: evidence/conversations.md (+ evidence/conversations.json).  Every answer comes from tool results only.

  python scripts/demo.py [--live]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MLFLOW_TRACKING_URI", f"sqlite:///{(ROOT / 'mlflow.db').as_posix()}")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from agent.config import Settings  # noqa: E402
from agent.orchestrator import Orchestrator, build_registry  # noqa: E402
from agent.schemas import ChatRequest  # noqa: E402
from agent.tools.airflow_tools import AirflowClient  # noqa: E402
from tests.fakes import FakeAirflowSession, FakeApps, FakeCore, k8s_factory, k8s_pod  # noqa: E402

OUT = ROOT / "evidence"
LOG: list[dict] = []


def agent_for(live: bool, *, k8s: tuple[FakeCore, FakeApps] | None = None, airflow: str = "ok") -> tuple[Orchestrator, str]:
    s = Settings(anthropic_api_key=None, airflow_password="demo", tool_timeout_s=30)
    kw: dict = {}
    mode = "REAL: DVC, MLflow | MOCKED: Kubernetes, Airflow"
    if not live:
        core, apps = k8s or (FakeCore([k8s_pod("inference-api-6d9f-abcde"), k8s_pod("inference-api-6d9f-fghij")]), FakeApps())
        kw["k8s_api_factory"] = k8s_factory(core, apps)
        kw["airflow_client"] = AirflowClient("http://mock-airflow", "admin", "demo", session=FakeAirflowSession(airflow))
    else:
        mode = "LIVE: DVC, MLflow, Airflow, Kubernetes"
        s.airflow_password = os.getenv("AIRFLOW_PASSWORD", "")
    return Orchestrator(build_registry(s, **kw), s), mode


def converse(title: str, question: str, agent: Orchestrator, mode: str, confirm: bool = False) -> None:
    r = agent.handle(ChatRequest(message=question, confirm=confirm))
    print(f"\n=== {title}\nUSER: {question}\n{r.answer}\n")
    LOG.append({"title": title, "backends": mode, "question": question, "confirm": confirm, "response": r.model_dump()})


def sh(*cmd: str) -> str:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def wait_dag_idle(agent: Orchestrator, timeout: int = 420) -> None:
    """Live mode: block until no Airflow run is queued/running (real polling through the agent's own tool)."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        r = agent.reg.execute("get_dag_run_status", {"limit": 3})
        if r.ok and not any(x["state"] in ("queued", "running") for x in r.data["runs"]):
            return
        time.sleep(10)


def trigger_live(agent: Orchestrator, title: str, conf: dict, mode: str) -> None:
    r = agent.reg.execute("trigger_training_dag", {"conf": conf}, confirmed=True)
    LOG.append({"title": title, "backends": mode, "question": f"(tool call) trigger_training_dag conf={conf} confirm=true", "confirm": True,
                "response": {"answer": f"{r.status}: {r.data or r.error}", "tool_calls": [r.model_dump()]}})
    print(f"\n=== {title}\n{r.status}: {r.data or r.error}")
    wait_dag_idle(agent)


def kubectl(*args: str) -> str:
    out = sh("kubectl", *args)
    print(f"$ kubectl {' '.join(args)}\n{out}")
    return out


def write_report() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "conversations.json").write_text(json.dumps(LOG, indent=2, default=str), encoding="utf-8")
    md = ["# Agent conversations with tool evidence\n"]
    for i, c in enumerate(LOG, 1):
        md.append(f"## {i}. {c['title']}\n\n*Backends: {c['backends']}*\n\n**User:** {c['question']}" + (" *(confirm=true)*" if c["confirm"] else "") + "\n")
        md.append("**Tool calls:**\n")
        for t in c["response"]["tool_calls"]:
            md.append(f"- `{t['tool']}` -> **{t['status']}** ({t['duration_ms']} ms)")
        md.append("\n**Agent answer:**\n\n" + c["response"]["answer"] + "\n")
    (OUT / "conversations.md").write_text("\n".join(md), encoding="utf-8")


def main() -> None:
    live = "--live" in sys.argv
    a, m = agent_for(live)
    # ---- the five assessment scenarios
    converse("Scenario 1 - Dataset provenance", os.getenv("DEMO_EXPERIMENT_QUESTION", "Which dataset version was used for experiment 4?"), a, m)
    converse("Scenario 1b - Dataset versions compared", "Compare dataset versions v1.0 and v2.0", a, m)
    converse("Scenario 1c - Reproduce an old dataset (explanation)", "Explain how to reproduce dataset version v1.0", a, m)
    converse("Scenario 2 - Model selection", "Compare the last five training runs and recommend the best candidate based on F1 score.", a, m)
    converse("Scenario 3a - Pipeline trigger without confirmation (safety)", "Trigger the model training pipeline and report whether execution succeeded.", a, m)
    converse("Scenario 3b - Pipeline execution (confirmed)", "Trigger the model training pipeline and report whether execution succeeded.", a, m, confirm=True)
    if live:
        wait_dag_idle(a)
    converse("Scenario 3c - Pipeline status", "What is the status of the training pipeline DAG runs?", a, m)
    converse("Scenario 4 - Deployment health", "Check the deployment status in Kubernetes", a, m)
    converse("Scenario 5 - Multi-tool reasoning",
             "Find the best recent model, identify the dataset version used to train it, check the deployment status, and explain whether retraining is necessary.", a, m)

    # ---- injected failure 1: data corruption (REAL DVC + REAL pipeline validation)
    print("\n##### INJECTING FAILURE 1: corrupt the DVC-tracked dataset (NaNs in column f3)")
    sh(sys.executable, "scripts/make_dataset.py", "--version", "2", "--corrupt")
    a1, m1 = agent_for(live, airflow="failed_validation")
    if live:  # the Airflow container reads the same (now corrupted) data file through its bind mount -> genuine validation failure
        trigger_live(a1, "Failure 1 - trigger DAG on the corrupted data", {}, m1)
    converse("Failure 1a - Has the data changed?", "Has the tracked dataset changed since it was versioned?", a1, m1)
    from training.pipeline import DataValidationError, validate
    try:
        validate()
    except DataValidationError as exc:
        print(f"REAL pipeline validate() -> DataValidationError: {exc}")
        LOG.append({"title": "Failure 1 - real validate() output", "backends": "REAL pipeline code", "question": "(direct call) validate()", "confirm": False,
                    "response": {"answer": f"DataValidationError: {exc}", "tool_calls": []}})
    converse("Failure 1b - Why did the training pipeline fail?", "Why did the training pipeline fail?", a1, m1)
    converse("Failure 1c - Resolve: restore the versioned data (needs confirm)", "Restore dataset version v2.0", a1, m1)
    converse("Failure 1d - Restore confirmed", "Restore dataset version v2.0", a1, m1, confirm=True)
    converse("Failure 1e - Verify the fix", "Has the tracked dataset changed since it was versioned?", a1, m1)

    # ---- injected failure 2: bad model path in the ConfigMap -> readiness probe fails, pods never ready (mocked cluster)
    print("\n##### INJECTING FAILURE 2: ConfigMap MODEL_PATH points to a missing file")
    if live:
        kubectl("-n", "mlops-agent", "patch", "configmap", "inference-config", "--type", "merge", "-p", '{"data":{"MODEL_PATH":"/models/model_v9.joblib"}}')
        converse("Failure 2 - restart without confirmation", "Restart the inference deployment in kubernetes", a, m)
        converse("Failure 2 - restart confirmed", "Restart the inference deployment in kubernetes", a, m, confirm=True)
        import time
        time.sleep(45)
        converse("Failure 2 - Why is my prediction API unavailable?", "Why is my prediction API unavailable?", a, m)
        kubectl("-n", "mlops-agent", "patch", "configmap", "inference-config", "--type", "merge", "-p", '{"data":{"MODEL_PATH":"/models/model.joblib"}}')
        a.handle(ChatRequest(message="Restart the inference deployment in kubernetes", confirm=True))
        kubectl("-n", "mlops-agent", "rollout", "status", "deploy/inference-api", "--timeout=150s")
        converse("Failure 2 - verify fix", "Check the deployment status in Kubernetes", a, m)
    ev = [NS(type="Warning", reason="Unhealthy", message="Readiness probe failed: HTTP probe failed with statuscode: 503", count=14)]
    core = FakeCore([k8s_pod("inference-api-7c8d-aaaaa", ready=False, restarts=0), k8s_pod("inference-api-7c8d-bbbbb", ready=False, restarts=0)],
                    logs="INFO Started server process\nERROR MODEL LOAD FAILED: FileNotFoundError: [Errno 2] No such file or directory: '/models/model_v9.joblib'\n"
                         "ERROR readiness failed: FileNotFoundError: /models/model_v9.joblib",
                    events=ev, endpoints=0)
    if not live:
        a2, m2 = agent_for(live, k8s=(core, FakeApps(ready=0)))
        converse("Failure 2 - Why is my prediction API unavailable?", "Why is my prediction API unavailable?", a2, m2)

    # ---- injected failure 3 (bonus): REAL quality-gate failure that already exists in MLflow (run 6, degraded model)
    a3, m3 = agent_for(live, airflow="quality_gate")
    if live:
        trigger_live(a3, "Failure 3 - trigger DAG with a degraded model", {"degrade_model": True}, m3)
    converse("Failure 3 - Quality gate blocks promotion", "Compare the last 6 mlflow runs on f1 and explain why the pipeline failed.", a3, m3)
    # ---- safety: unauthorized K8s change
    a4, m4 = agent_for(live)
    r = a4.reg.execute("restart_k8s_deployment", {"namespace": "kube-system", "deployment": "coredns"}, confirmed=True)
    LOG.append({"title": "Safety - restart in an unapproved namespace", "backends": m4, "question": "restart coredns in kube-system (confirm=true)",
                "confirm": True, "response": {"answer": f"{r.status}: {r.error}", "tool_calls": [r.model_dump()]}})
    print(f"\nSafety: {r.status}: {r.error}")
    write_report()
    print("\nwrote evidence/conversations.md")


if __name__ == "__main__":
    main()
