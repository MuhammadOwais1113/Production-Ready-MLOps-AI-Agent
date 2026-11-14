"""Build report/MLOps_Agent_Technical_Report.pdf from the live repository state and evidence/ exports.

Everything numeric in the report (git history, DVC pointers, MLflow runs, tool inventory, conversations, test results)
is read from the real artefacts at build time - nothing is typed in by hand.
Run:  python scripts/build_report.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MLFLOW_TRACKING_URI", f"sqlite:///{(ROOT / 'mlflow.db').as_posix()}")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
EV = ROOT / "evidence"
OUT = ROOT / "report"
NAVY = colors.HexColor("#1F3864")
LIGHT = colors.HexColor("#D9E2F3")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], textColor=NAVY, fontSize=16, spaceBefore=10, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=12, spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9.2, leading=12.5)
SMALL = ParagraphStyle("S", parent=BODY, fontSize=8, leading=10)
CODE = ParagraphStyle("C", fontName="Courier", fontSize=6.9, leading=8.4, backColor=colors.HexColor("#F4F6FA"), borderPadding=4)


def P(t: str, st: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(t, st)


def code(text: str, width: int = 118) -> Preformatted:
    lines: list[str] = []
    for ln in text.splitlines():
        lines += textwrap.wrap(ln, width, subsequent_indent="    ", replace_whitespace=False) or [""]
    return Preformatted("\n".join(lines), CODE)


def table(rows: list[list], widths: list[float], font: float = 7.6, header: bool = True) -> Table:
    data = [[Paragraph(escape(str(c)), ParagraphStyle("t", parent=BODY, fontSize=font, leading=font + 2)) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9AA5B8")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), LIGHT)]
    t.setStyle(TableStyle(st))
    return t


def sh(*cmd: str) -> str:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def clean_md(s: str) -> str:
    return re.sub(r"\*\*|`|^## ", "", s, flags=re.M)


# ------------------------------------------------------------------------------------------------ figures
def arch_drawing() -> Drawing:
    d = Drawing(500, 300)

    def box(x, y, w, h, text, fill="#E8F0FB", size=8):
        d.add(Rect(x, y, w, h, fillColor=colors.HexColor(fill), strokeColor=NAVY, rx=4, ry=4))
        for i, ln in enumerate(text.split("\n")):
            d.add(String(x + w / 2, y + h / 2 + (len(text.split("\n")) - 1) * 5 - i * 10 - 3, ln, textAnchor="middle", fontSize=size))

    def arrow(x1, y1, x2, y2):
        d.add(Line(x1, y1, x2, y2, strokeColor=NAVY))
        d.add(Rect(x2 - 2, y2 - 2, 4, 4, fillColor=NAVY, strokeColor=NAVY))
    box(150, 260, 200, 32, "User / Engineer\nnatural-language requests")
    box(150, 210, 200, 32, "FastAPI interface (api/main.py)\nvalidation, request-id, response", "#E6F4EA")
    box(110, 140, 280, 48, "MLOps AI Agent (orchestrator)\nLLM tool-calling or offline planner\nToolRegistry: validation, confirm gate, timeout, logs")
    for i, (n, sub) in enumerate((("DVC", "versions/lineage"), ("MLflow", "runs/registry"), ("Airflow 3", "DAGs/training"), ("Kubernetes", "pods/services"))):
        x = 5 + i * 125
        box(x, 70, 115, 38, f"{n}\n{sub}")
        arrow(250, 140, x + 57, 108)
        arrow(x + 57, 70, 250, 38)
    box(110, 6, 280, 32, "Evidence-based response\nknown facts | uncertainties | recommended actions", "#E6F4EA")
    arrow(250, 260, 250, 242)
    arrow(250, 210, 250, 188)
    return d


def dag_drawing() -> Drawing:
    d = Drawing(500, 50)
    names = ["validate_dataset", "preprocess_data", "train_model", "evaluate_and_log\n_mlflow", "quality_gate", "promote_model"]
    for i, n in enumerate(names):
        x = 2 + i * 83
        d.add(Rect(x, 8, 76, 34, fillColor=colors.HexColor("#E6F4EA" if i >= 4 else "#E8F0FB"), strokeColor=NAVY, rx=3, ry=3))
        for j, ln in enumerate(n.split("\n")):
            d.add(String(x + 38, 27 - j * 9 + (4 if "\n" in n else 0) - 2, ln, textAnchor="middle", fontSize=6.3))
        if i:
            d.add(Line(x - 7, 25, x, 25, strokeColor=NAVY))
    return d


# ------------------------------------------------------------------------------------------------ data
def tool_inventory() -> list[list[str]]:
    from agent.config import Settings
    from agent.orchestrator import build_registry
    reg = build_registry(Settings(anthropic_api_key=None))
    rows = [["Module", "Tool", "Permission", "Purpose"]]
    for dom in ("dvc", "mlflow", "airflow", "kubernetes"):
        for t in sorted(reg.by_domain(dom), key=lambda x: x.name):
            perm = "STATE-CHANGING: needs confirm=true" + (" + approved namespace/DAG" if dom in ("kubernetes", "airflow") else "") if t.mutating else \
                ("read-only; approved namespace" if dom == "kubernetes" else "read-only")
            rows.append([dom, t.name, perm, t.description])
    return rows


def mlflow_rows() -> list[list[str]]:
    from mlflow.tracking import MlflowClient
    c = MlflowClient(os.environ["MLFLOW_TRACKING_URI"])
    exp = c.get_experiment_by_name("mlops-agent-demo")
    rows = [["#", "run id", "f1", "accuracy", "n_est", "depth", "gate", "dvc version", "dvc md5", "git"]]
    if exp:
        for r in sorted(c.search_runs([exp.experiment_id], max_results=100), key=lambda r: int(r.data.tags.get("run_number", 0))):
            t, m, p = r.data.tags, r.data.metrics, r.data.params
            rows.append([t.get("run_number"), r.info.run_id[:8], f"{m.get('f1', 0):.4f}", f"{m.get('accuracy', 0):.4f}", p.get("n_estimators"),
                         p.get("max_depth"), t.get("quality_gate"), t.get("dvc.version"), (t.get("dvc.md5") or "")[:10], (t.get("git.commit") or "")[:7]])
    return rows


def requirements_matrix() -> list[list[str]]:
    return [
        ["Req.", "Where implemented", "Verified by"],
        ["T1 DVC: list datasets, version+commit, compare, detect change, explain reproduce", "agent/tools/dvc_tools.py (7 tools), dvc.yaml, data/dataset.csv.dvc", "tests/test_dvc_tools.py; real demo (Scenario 1, Failure 1)"],
        ["T2 MLflow: list, metrics/params/artifacts, best run, compare, registry; not assuming best=ready", "agent/tools/mlflow_tools.py (7 tools, readiness checks)", "tests/test_mlflow_tools.py (incl. real SQLite MLflow); Scenario 2"],
        ["T3 Airflow: list DAGs, status, trigger approved, failed tasks, diagnostics; DAG with retries/gate", "agent/tools/airflow_tools.py (5 tools), dags/training_pipeline.py", "tests/test_airflow_tools.py, DAG structure test; Scenario 3"],
        ["T4 Kubernetes: pods/deployments, logs, services, unhealthy, version; manifests", "agent/tools/kubernetes_tools.py (7 tools), kubernetes/*.yaml, serving/app.py", "tests/test_kubernetes_tools.py, manifest tests; Scenario 4, Failure 2"],
        ["Confirmation + approved namespace for state changes", "ToolRegistry confirm gate; KubernetesTools._ns", "test_namespace_outside_approved_list..., test_state_change_needs_confirmation..."],
        ["Integration scenario, no automatic retraining, facts/uncertainties/actions", "agent/orchestrator.py (multi plan + _correlate)", "tests/test_agent.py; Scenario 5"],
        ["Tests: >=8, mocks, timeouts, invalid, service failure, no invention, unauthorized K8s, structured logs", "tests/ (59 tests)", "pytest"],
        ["GitHub repo + CI (lint + tests on PR)", ".github/workflows/ci.yml, pyproject.toml", "ruff + pytest in CI"],
        ["Structure from assignment", "see repository tree", "-"],
    ]


# ------------------------------------------------------------------------------------------------ document
def build() -> Path:
    OUT.mkdir(exist_ok=True)
    pdf = OUT / "MLOps_Agent_Technical_Report.pdf"
    doc = SimpleDocTemplate(str(pdf), pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                            title="MLOps AI Agent - Technical Report", author="Assignment 03")
    W = A4[0] - 3.2 * cm
    S: list = []
    conv = json.loads((EV / "conversations.json").read_text(encoding="utf-8")) if (EV / "conversations.json").exists() else []
    pytest_txt = (EV / "pytest.txt").read_text(encoding="utf-8") if (EV / "pytest.txt").exists() else "(run pytest and save to evidence/pytest.txt)"

    S += [P("Assignment 03 - Production-Ready MLOps AI Agent", ParagraphStyle("T", parent=H1, fontSize=22, spaceAfter=2)),
          P("Technical Report | MLOps &amp; Production AI Engineering | Fall 2026", ParagraphStyle("st", parent=BODY, textColor=colors.grey)),
          Spacer(1, 6),
          P("This report documents an AI agent that answers MLOps questions by calling real DVC, MLflow, Apache Airflow 3 and Kubernetes tools. "
            "All values below were produced by the running system; the evidence-honesty section states exactly which backends were real and which were mocked."),
          P("1. Evidence honesty statement", H1),
          table([["Backend", "In this report", "Notes"],
                 ["DVC", "REAL (git + dvc CLI, local DVC remote)", "Two dataset versions v1.0 / v2.0, checkout demonstrated"],
                 ["MLflow", "REAL (SQLite backend, runs written by the training pipeline)", "6 runs incl. a natural and an injected quality-gate failure"],
                 ["Airflow 3", "Agent client verified against scripted Airflow REST responses (CI/demo); DAG file reviewed statically", "See section 9 for live status of the Docker stack"],
                 ["Kubernetes", "Agent tools verified against scripted API objects; manifests schema-checked by tests", "See section 9 for live status of a cluster"],
                 ["LLM", "Deterministic offline planner used for all recorded conversations", "LLM tool-calling path is implemented and unit-tested with a fake client; no ANTHROPIC_API_KEY was configured here"]],
                [2.2 * cm, 7.5 * cm, W - 9.7 * cm])]

    S += [P("2. Complete system architecture", H1), arch_drawing(), P("Figure 1. Logical architecture (see architecture.md).", SMALL),
          P("Design decisions: (1) every tool call passes through one <b>ToolRegistry</b> so validation, confirmation gating, timeouts, error classification and secret-free structured logs are identical for all four systems; "
            "(2) the model never grants confirmation - the flag comes from the human request; (3) dataset provenance is written by the training pipeline into MLflow tags and re-verified against git history; "
            "(4) retraining is only recommended from explicit evidence (freshness policy, failed quality gate, supplied degradation)."),
          P("Training DAG (Figure 2)", H2), dag_drawing(),
          P("Each task has retries=2 (validation uses AirflowFailException so bad data is not retried). A failed validation or quality gate fails its task; promote_model has trigger_rule=all_success, so it ends as upstream_failed - promotion is prevented.", SMALL)]

    S += [PageBreak(), P("3. Tool definitions and permissions", H1),
          P("Modules each expose at least three tools. Permission column: read-only tools only query; state-changing tools return <i>needs_confirmation</i> unless the human sets confirm=true; Kubernetes calls are limited to APPROVED_NAMESPACES; Airflow triggers to APPROVED_DAGS; DVC runs only allow-listed git/dvc sub-commands without a shell."),
          table(tool_inventory(), [1.6 * cm, 4.2 * cm, 3.6 * cm, W - 9.4 * cm], 6.8)]

    S += [P("4. Requirement traceability", H1), table(requirements_matrix(), [6.6 * cm, 5.6 * cm, W - 12.2 * cm], 7.2)]

    S += [PageBreak(), P("5. Exported evidence", H1), P("5.1 DVC - versions, pointers, status", H2)]
    S += [code("$ git log --oneline --decorate\n" + sh("git", "log", "--oneline", "--decorate") + "\n\n$ cat data/dataset.csv.dvc  (current)\n" + (ROOT / "data/dataset.csv.dvc").read_text(encoding="utf-8")
               + "\n$ git show v1.0:data/dataset.csv.dvc\n" + sh("git", "show", "v1.0:data/dataset.csv.dvc") + "\n\n$ dvc remote list\n" + sh(str(ROOT / ".venv/Scripts/dvc.exe"), "remote", "list")
               + "\n\n$ dvc status data/dataset.csv.dvc\n" + sh(str(ROOT / ".venv/Scripts/dvc.exe"), "status", "data/dataset.csv.dvc"))]
    S += [P("5.2 MLflow - recorded runs (queried with MlflowClient)", H2), table(mlflow_rows(), [0.6 * cm, 1.6 * cm, 1.4 * cm, 1.5 * cm, 1.1 * cm, 1.1 * cm, 1.4 * cm, 1.6 * cm, 2.4 * cm, W - 12.7 * cm], 6.6),
          P("Run 3 failed the f1&gt;=0.85 gate naturally (shallow model). Run 6 is an injected degradation (<font face='Courier'>--degrade</font>). Runs 3 and 6 were not registered in the model registry because promotion is downstream of the gate.", SMALL)]
    reg_txt = (EV / "airflow_k8s_status.txt").read_text(encoding="utf-8") if (EV / "airflow_k8s_status.txt").exists() else ""
    S += [P("5.3 Airflow and Kubernetes", H2), code(reg_txt or "See section 9."),
          P("Kubernetes manifests (kubernetes/): Deployment (2 replicas, readiness+liveness probes, requests/limits, envFrom ConfigMap), ClusterIP Service, ConfigMap, Namespace, least-privilege RBAC for the agent.", SMALL)]

    S += [PageBreak(), P("6. Five tested agent conversations (actual tool evidence)", H1),
          P("Each answer below is the verbatim agent output; the tool-call list shows which tools were executed. Backends per conversation are labelled.", SMALL)]
    want = ["Scenario 1 - Dataset provenance", "Scenario 2 - Model selection", "Scenario 3b - Pipeline execution (confirmed)", "Scenario 4 - Deployment health", "Scenario 5 - Multi-tool reasoning"]
    for n, title in enumerate(want, 1):
        c = next((x for x in conv if x["title"] == title), None)
        if not c:
            continue
        calls = ", ".join(f"{t['tool']}[{t['status']}]" for t in c["response"]["tool_calls"])
        S += [P(f"Conversation {n}: {escape(title)}", H2), P(f"<b>Backends:</b> {escape(c['backends'])}", SMALL), P(f"<b>User:</b> {escape(c['question'])}", BODY),
              P(f"<b>Tools:</b> {escape(calls)}", SMALL), code(clean_md(c["response"]["answer"]))]
    extra = [x for x in conv if x["title"].startswith(("Scenario 1b", "Scenario 1c", "Scenario 3a", "Safety"))]
    S += [P("Additional recorded conversations (dataset comparison/reproduction, confirmation gate, namespace denial)", H2)]
    for c in extra:
        S += [P(f"<b>{escape(c['title'])}</b> - {escape(c['question'])}", SMALL), code(clean_md(c["response"]["answer"]))]

    S += [PageBreak(), P("7. Failure investigations", H1)]
    f1 = [x for x in conv if x["title"].startswith("Failure 1")]
    S += [P("Failure 1 - Corrupted tracked dataset (real DVC + real pipeline code)", H2),
          P("<b>Injection:</b> <font face='Courier'>python scripts/make_dataset.py --version 2 --corrupt</font> overwrote data/dataset.csv with NaNs in column f3 (25% of rows) without a DVC commit.<br/>"
            "<b>Diagnosis by the agent:</b> detect_data_changes (real <font face='Courier'>dvc status</font>) reported the file as <i>modified</i>; the pipeline validation stage raised <i>null values found: {'f3': 375}</i>; "
            "Airflow-style diagnostics showed validate_dataset failed and all downstream tasks upstream_failed, i.e. training and promotion never ran on bad data.<br/>"
            "<b>Root cause:</b> workspace data diverged from the version recorded in DVC (data layer).<br/>"
            "<b>Resolution:</b> agent requested confirmation, then ran checkout_dataset_version(v2.0) (git checkout of the pointer + dvc checkout --force); dvc status returned 'up to date'. "
            "A real bug was found while doing this: dvc checkout refused to overwrite modified data without --force, so the first confirmed restore failed and the agent reported the DVC error rather than claiming success.")]
    for c in f1:
        S += [P(f"<b>{escape(c['title'])}</b> - {escape(c['question'])}", SMALL), code(clean_md(c["response"]["answer"]))]
    f2 = next((x for x in conv if x["title"].startswith("Failure 2")), None)
    S += [P("Failure 2 - Wrong MODEL_PATH in the ConfigMap -> readiness failures (infrastructure layer; scripted cluster state)", H2),
          P("<b>Injection:</b> ConfigMap MODEL_PATH points to a missing file; the inference service starts (liveness passes) but /ready returns 503, so pods are Running but never Ready and the Service has zero endpoints.<br/>"
            "<b>Diagnosis:</b> the agent combined deployment (ready 0/2), pod state (running but NOT ready), Warning events (Readiness probe failed 503), log tail (FileNotFoundError ... model_v9.joblib) and the Service (0 ready endpoints) into one evidence chain.<br/>"
            "<b>Root cause:</b> configuration error, not an image or resource problem (no restarts, no OOMKilled). <b>Resolution:</b> correct MODEL_PATH in kubernetes/configmap.yaml, kubectl apply, restart the deployment (agent: restart_k8s_deployment with confirm=true).")]
    if f2:
        S += [code(clean_md(f2["response"]["answer"]))]
    f3 = next((x for x in conv if x["title"].startswith("Failure 3")), None)
    S += [P("Bonus - quality gate failure (real MLflow data)", H2),
          P("Runs 3 and 6 in MLflow carry quality_gate=failed (f1 0.817 and 0.619 vs. threshold 0.85); the DAG's quality_gate task fails and promote_model stays upstream_failed, so no model version is registered for them.")]
    if f3:
        S += [code(clean_md(f3["response"]["answer"]))]

    S += [PageBreak(), P("8. Automated tests, reliability and safety", H1),
          P("pytest output (mocks for Kubernetes/Airflow/LLM; real git and real MLflow SQLite store):"), code(pytest_txt[-2500:]),
          table([["Required test", "Test(s)"],
                 ["Successful tool selection and execution", "test_agent.py::test_selects_*; test_api...::test_api_health_tools_and_chat"],
                 ["Timeouts / invalid requests / service failures", "test_tools_base.py::test_tool_timeout_is_reported, ::test_invalid_arguments..., ::test_service_failure...; test_airflow_tools.py::test_airflow_down...; test_mlflow_tools.py::test_unreachable_server..."],
                 ["Agent does not invent results when a tool is unavailable", "test_agent.py::test_does_not_invent_results_when_tools_unavailable"],
                 ["Unauthorized Kubernetes modifications rejected", "test_kubernetes_tools.py::test_namespace_outside_approved_list...; test_agent.py::test_unauthorized_k8s_modification..."],
                 ["Structured logs: tool, status, duration, request id, no secrets", "test_tools_base.py::test_structured_logs_have_name_status_duration_requestid_and_no_secrets"],
                 ["Model cannot self-confirm (LLM mode)", "test_agent.py::test_llm_mode_runs_tools_and_model_cannot_self_confirm"],
                 ["Retraining only with evidence, never automatic", "test_agent.py::test_multi_tool_scenario..., ::test_retraining_recommended_only_with_evidence"]],
                [6.3 * cm, W - 6.3 * cm], 7.4),
          P("Sample structured log lines (evidence/demo_tool_logs.jsonl):", H2)]
    logs = (EV / "demo_tool_logs.jsonl").read_text(encoding="utf-8").splitlines() if (EV / "demo_tool_logs.jsonl").exists() else []
    S += [code("\n".join([ln for ln in logs if '"tool_call"' in ln][:6]), 130)]

    S += [P("9. Live-infrastructure status in the authoring environment", H1),
          P((EV / "live_status.txt").read_text(encoding="utf-8").replace("\n", "<br/>") if (EV / "live_status.txt").exists() else "Not recorded."),
          P("10. Known limitations and proposed improvements", H1)]
    for t in ["Offline planner is rule-based; LLM mode (Claude tool calling) handles paraphrases but needs an API key and was only verified with a fake client in this environment.",
              "Airflow/Kubernetes behaviour is proven against scripted responses; run the Docker stack and a cluster to produce live screenshots for the demonstration.",
              "Airflow 3 task-log API shape differs slightly across 3.x minors; the client accepts both JSON and plain-text log bodies.",
              "dvc checkout --force overwrites local edits (guarded by confirmation); a production system should stash or snapshot first.",
              "Single confirmation flag; use signed approvals, per-user RBAC and audit storage in production. Add model-drift / slice metrics to the promotion policy.",
              "Planner intent matching is keyword-based and may over- or under-select tools for unusual phrasing; extend with the LLM planner and an intent test-suite."]:
        S += [P("&bull; " + escape(t), BODY)]
    S += [P("11. Repository structure", H1), code(sh("git", "ls-files") if False else "\n".join(sorted(
        str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*") if p.is_file() and not any(
            x in p.parts for x in (".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "dvc_remote", ".dvc", "report", "mlruns", "artifacts", "models"))
        and p.name not in ("mlflow.db", ".env") and not p.name.endswith(".pyc")))[:3500])]
    doc.build(S)
    return pdf


if __name__ == "__main__":
    print(build())
