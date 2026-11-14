"""Agent orchestrator.

Two interchangeable planners share one ToolRegistry (so permissions, logging and timeouts are identical):
  * LLM mode  - Claude chooses tools via native tool calling (needs ANTHROPIC_API_KEY).
  * Offline   - a deterministic, rule-based planner. Used when no key is set or the LLM call fails.
Both end in an evidence-based report that separates known facts, uncertainties and recommended actions.
Operational claims are only ever produced from ToolResult data; a tool that is not 'ok' becomes an uncertainty."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from agent.config import Settings
from agent.logging_utils import get_logger, log_event
from agent.schemas import ChatRequest, ChatResponse, Evidence, ToolResult
from agent.tools.airflow_tools import AirflowClient, AirflowTools
from agent.tools.base import ToolRegistry
from agent.tools.dvc_tools import DvcTools
from agent.tools.kubernetes_tools import KubernetesTools, load_apis
from agent.tools.mlflow_tools import MlflowTools, make_client_factory

logger = get_logger("mlops_agent.orchestrator")

SYSTEM_PROMPT = """You are an MLOps assistant with tools for DVC (data versions), MLflow (experiments), Apache Airflow (pipelines) and Kubernetes (deployment).
Rules:
1. Every operational claim (metrics, versions, statuses, logs) MUST come from a tool result in this conversation. Never answer from memory or guess.
2. If a tool returns status other than 'ok', say that the information is unavailable and why. Do not fabricate or estimate values.
3. Never assume the highest metric means a model is production-ready; use the readiness checks returned by the tools.
4. State-changing tools (trigger DAG, restart/scale deployment, dataset checkout) need explicit user confirmation, which you cannot grant. If a tool replies needs_confirmation, tell the user to confirm.
5. Never trigger retraining unless the user explicitly asks to trigger it. Recommend retraining only with evidence: an explicit freshness policy breach, a failed quality gate, or degradation results supplied by the user.
6. Structure the final answer in three sections: **Known facts**, **Uncertainties**, **Recommended actions**. Keep it concise.
"""

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def build_registry(settings: Settings | None = None, *, k8s_api_factory=None, airflow_client: AirflowClient | None = None,
                   mlflow_factory=None, dvc_runner=None) -> ToolRegistry:
    s = settings or Settings()
    reg = ToolRegistry(timeout_s=s.tool_timeout_s)
    mf = mlflow_factory or make_client_factory(s.mlflow_tracking_uri)
    reg.register(DvcTools(s.repo_root, s.dvc_data_files, runner=dvc_runner, mlflow_client_factory=mf,
                          experiment=s.mlflow_experiment).tools())
    reg.register(MlflowTools(mf, s.mlflow_experiment, s.registered_model, s.quality_gate_f1, s.max_model_age_days).tools())
    ac = airflow_client or AirflowClient(s.airflow_url, s.airflow_username, s.airflow_password)
    reg.register(AirflowTools(ac, s.approved_dags).tools())
    reg.register(KubernetesTools(k8s_api_factory or load_apis, s.approved_namespaces, s.k8s_namespace, s.k8s_app_label,
                                 s.k8s_deployment, s.max_replicas).tools())
    return reg


class Orchestrator:
    def __init__(self, registry: ToolRegistry, settings: Settings | None = None, llm_client: Any | None = None) -> None:
        self.reg = registry
        self.s = settings or Settings()
        self._llm = llm_client

    # ------------------------------------------------------------------ public
    def handle(self, req: ChatRequest) -> ChatResponse:
        rid = req.request_id or uuid.uuid4().hex[:12]
        log_event(logger, "request_received", request_id=rid, chars=len(req.message), confirm=req.confirm)
        llm = self._llm or self._make_llm()
        if llm is not None:
            try:
                return self._run_llm(llm, req, rid)
            except Exception as exc:  # noqa: BLE001
                log_event(logger, "llm_failed_fallback_offline", request_id=rid, error=f"{type(exc).__name__}: {exc}"[:200])
        return self._run_offline(req, rid)

    # --------------------------------------------------------------- LLM mode
    def _make_llm(self) -> Any | None:
        if not self.s.anthropic_api_key:
            return None
        import anthropic
        return anthropic.Anthropic(api_key=self.s.anthropic_api_key)

    def _run_llm(self, llm: Any, req: ChatRequest, rid: str) -> ChatResponse:
        messages: list[dict[str, Any]] = [{"role": "user", "content": req.message}]
        calls: list[ToolResult] = []
        text = ""
        for _ in range(self.s.max_steps):
            resp = llm.messages.create(model=self.s.model, max_tokens=2000, system=SYSTEM_PROMPT,
                                       tools=self.reg.schemas(), messages=messages)
            blocks = list(resp.content)
            text = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
            uses = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
            if not uses:
                break
            messages.append({"role": "assistant", "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in blocks]})
            results = []
            for u in uses:
                # confirmation comes from the human request flag only - the model cannot grant it
                r = self.reg.execute(u.name, dict(u.input), request_id=rid, confirmed=req.confirm)
                calls.append(r)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(r.model_dump(), default=str)[:12000],
                                "is_error": not r.ok})
            messages.append({"role": "user", "content": results})
        notes = [f"- `{c.tool}` returned **{c.status}**: {c.error}" for c in calls if not c.ok]
        if notes:
            text += "\n\n**Tool availability notes (automatic):**\n" + "\n".join(notes)
        if not calls:
            text += "\n\n_No tools were called, so this reply contains no verified operational data._"
        return ChatResponse(request_id=rid, mode="llm", answer=text.strip(), tool_calls=calls,
                            evidence=[Evidence(tool=c.tool, status=c.status, summary=_one_line(c)) for c in calls])

    # ----------------------------------------------------------- offline mode
    def _run_offline(self, req: ChatRequest, rid: str) -> ChatResponse:
        msg = req.message.strip()
        plan = self.plan(msg)
        calls: list[ToolResult] = []
        ctx: dict[str, Any] = {"message": msg}

        def call(name: str, args: dict[str, Any] | None = None) -> ToolResult:
            r = self.reg.execute(name, args or {}, request_id=rid, confirmed=req.confirm)
            calls.append(r)
            ctx[name] = r
            return r

        run_id: str | None = None
        for step in plan["steps"]:
            name, args = step
            if name == "get_experiment_dataset" and not args and run_id:
                args = {"run_id": run_id}
            r = call(name, args)
            if name in ("find_mlflow_run", "find_best_mlflow_run") and r.ok:
                run_id = r.data["best"]["run_id"]
            if name == "compare_mlflow_runs" and r.ok and r.data.get("recommended"):
                run_id = r.data["recommended"]
        facts, unc, acts = _correlate(ctx, self.s, plan)
        answer = _render(plan["title"], facts, unc, acts)
        return ChatResponse(request_id=rid, mode="offline-planner", answer=answer, known_facts=facts,
                            uncertainties=unc, recommended_actions=acts, tool_calls=calls,
                            evidence=[Evidence(tool=c.tool, status=c.status, summary=_one_line(c)) for c in calls])

    # ---------------------------------------------------------------- planner
    def plan(self, msg: str) -> dict[str, Any]:
        m = msg.lower()
        has = lambda *w: any(re.search(rf"\b{x}", m) for x in w)  # noqa: E731
        d_mlflow = has("mlflow", "best", "model", "metric", "f1", "accuracy", "runs?", "experiments?", "registry", "candidate")
        d_dvc = has("dvc", "dataset", "data version", "provenance", "data changed", "data drift", "reproduce")
        d_air = has("airflow", "dag", "pipeline", "workflow", "retrain\\w*", "failed task")
        d_k8s = has("kubernetes", "k8s", "pod", "pods", "deployment", "deployed", "service", "api unavailable", "unavailable",
                    "prediction api", "namespace", "replicas?", "restart")
        retrain_q = has("retrain\\w*") and has("whether", "necessary", "need", "should", "required")
        trigger = bool(re.search(r"\b(trigger|start|launch|kick off|kickoff|run)\b.*\b(pipeline|dag|training)\b", m)) and not retrain_q
        steps: list[tuple[str, dict[str, Any]]] = []
        metric = next((x for x in ("f1", "accuracy", "precision", "recall") if x in m), "f1")
        n = None
        mm = re.search(r"last (\d+|" + "|".join(_NUM) + r")", m)
        if mm:
            n = int(mm.group(1)) if mm.group(1).isdigit() else _NUM[mm.group(1)]
        revs = re.findall(r"\bv\d+(?:\.\d+)*\b", m)
        run_no = re.search(r"(?:experiment|run)\s*#?(\d+)", m)

        # Integration / multi-tool: best model + dataset + deployment (+ retraining question)
        multi = sum([d_mlflow, d_dvc, d_k8s, d_air]) >= 3 or (retrain_q and (d_mlflow or d_k8s or d_dvc))
        if multi and not trigger:
            return {"title": "Model health & retraining review", "kind": "multi", "steps": [
                ("find_best_mlflow_run", {"metric": metric}), ("get_experiment_dataset", {}), ("get_dataset_version", {}),
                ("detect_data_changes", {}), ("get_deployed_version", {}), ("find_unhealthy_pods", {}),
                ("get_dag_run_status", {"limit": 5}), ("get_failed_tasks", {})]}

        if d_k8s and has("restart") and not has("restarts"):
            steps.append(("restart_k8s_deployment", {}))
        elif d_k8s and has("scale") and re.search(r"(\d+)\s*replicas?|to\s+(\d+)", m):
            g = re.search(r"(\d+)\s*replicas?|to\s+(\d+)", m)
            steps.append(("scale_k8s_deployment", {"replicas": int(g.group(1) or g.group(2))}))
        else:
            if (d_mlflow and (not d_air or has("best", "compare", "registry", "mlflow", "f1", "accuracy", "metrics?"))) and not (d_dvc and run_no):
                if has("registry", "model version", "versions"):
                    steps.append(("get_model_registry_info", {}))
                elif has("compare"):
                    steps.append(("compare_mlflow_runs", {"last_n": n or 5, "metric": metric}))
                elif has("best", "recommend", "candidate"):
                    steps.append(("find_best_mlflow_run", {"metric": metric}))
                elif has("experiments") and not has("runs"):
                    steps.append(("list_mlflow_experiments", {}))
                elif has("detail", "artifact", "param"):
                    steps.append(("get_mlflow_run_details", {}))
                else:
                    steps.append(("list_mlflow_runs", {"limit": n or 5}))
            if d_dvc:
                if run_no and has("experiment", "run"):
                    steps.append(("get_experiment_dataset", {"run_number": int(run_no.group(1))}))
                elif len(revs) >= 2 and has("compare", "diff", "differ"):
                    steps.append(("compare_dataset_versions", {"rev_a": revs[0], "rev_b": revs[1]}))
                elif has("checkout", "restore") and not has("how", "explain", "steps"):
                    steps.append(("checkout_dataset_version", {"rev": revs[0] if revs else "HEAD"}))
                elif has("reproduce", "restore", "recreate", "checkout"):
                    steps.append(("explain_dataset_reproduction", {"rev": revs[0] if revs else "HEAD"}))
                elif has("changed", "change", "modified", "drift", "tamper"):
                    steps.append(("detect_data_changes", {}))
                elif has("list", "which datasets", "tracked"):
                    steps.append(("list_dvc_datasets", {}))
                elif steps and steps[-1][0] in ("find_best_mlflow_run", "compare_mlflow_runs"):
                    steps.append(("get_experiment_dataset", {}))
                else:
                    steps.append(("get_dataset_version", {}))
            if d_air:
                if trigger:
                    steps.append(("trigger_training_dag", {}))
                elif has("list") and has("dags"):
                    steps.append(("list_airflow_dags", {}))
                elif has("fail", "failed", "why", "diagnose", "error", "broke"):
                    steps += [("get_dag_run_status", {"limit": 3}), ("get_failed_tasks", {}), ("get_task_diagnostics", {})]
                else:
                    steps.append(("get_dag_run_status", {"limit": 5}))
            if d_k8s:
                if has("version"):
                    steps.append(("get_deployed_version", {}))
                elif has("unavailable", "why", "diagnos", "unhealthy", "crash", "not working", "down", "log", "logs", "restarts"):
                    steps += [("list_k8s_workloads", {}), ("find_unhealthy_pods", {}), ("get_pod_status_and_logs", {}),
                              ("inspect_k8s_services", {})]
                elif has("service"):
                    steps.append(("inspect_k8s_services", {}))
                else:
                    steps += [("list_k8s_workloads", {}), ("find_unhealthy_pods", {})]
        title = "MLOps agent report"
        if not steps:
            return {"title": "No tool matched", "kind": "none", "steps": []}
        return {"title": title, "kind": "single", "steps": steps}


# ----------------------------------------------------------------- reporting
def _one_line(r: ToolResult) -> str:
    if not r.ok:
        return f"{r.status}: {r.error}"
    s = json.dumps(r.data, default=str)
    return s if len(s) < 220 else s[:217] + "..."


def _render(title: str, facts: list[str], unc: list[str], acts: list[str]) -> str:
    def sec(h: str, items: list[str]) -> str:
        return f"**{h}**\n" + ("\n".join(f"- {i}" for i in items) if items else "- (none)")
    return f"## {title}\n\n" + "\n\n".join([sec("Known facts (from tool evidence)", facts), sec("Uncertainties", unc),
                                            sec("Recommended actions", acts)])


def _fmt(v: Any) -> str:
    return f"{v:.4f}" if isinstance(v, (int, float)) else "n/a"


def _get(ctx: dict[str, Any], tool: str) -> ToolResult | None:
    return ctx.get(tool)


def _correlate(ctx: dict[str, Any], s: Settings, plan: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    facts: list[str] = []
    unc: list[str] = []
    acts: list[str] = []
    if plan["kind"] == "none":
        return ([], ["The request did not map to any available tool (DVC, MLflow, Airflow, Kubernetes), so nothing was queried."],
                ["Rephrase with a concrete operational question, e.g. 'Which dataset version was used for experiment 3?'"])
    retrain_evidence: list[str] = []
    for name, r in list(ctx.items()):
        if name == "message":
            continue
        if not r.ok:
            tag = {"needs_confirmation": "needs your confirmation", "denied": "denied by policy", "timeout": "timed out",
                   "unavailable": "unavailable", "invalid": "invalid request", "error": "failed"}.get(r.status, r.status)
            unc.append(f"`{name}` {tag}: {r.error}. No claims are made about this part.")
            if r.status == "needs_confirmation":
                acts.append("Re-send the same request with confirm=true if you really want this state-changing action.")
            continue
        d = r.data
        if name == "find_best_mlflow_run":
            b, rd = d["best"], d["readiness"]
            facts.append(f"Best of the {d['runs_considered']} most recent finished runs by {d['metric']}: run #{b['run_number']} "
                         f"({b['run_id'][:8]}) {d['metric']}={b['metrics'][d['metric']]:.4f}; all metrics {json.dumps({k: round(v, 4) for k, v in b['metrics'].items()})}.")
            if d["runner_up_margin"] is not None:
                facts.append(f"Margin over the runner-up: {d['runner_up_margin']}.")
            facts.append(f"Readiness verdict: {rd['verdict']}.")
            for c in rd["checks"]:
                if not c["ok"]:
                    unc.append(f"Readiness check failed - {c['check']}: {c['detail']}.")
            if b.get("quality_gate") == "failed":
                retrain_evidence.append("the best run's quality gate FAILED")
            if rd["age_days"] > s.max_model_age_days:
                retrain_evidence.append(f"model is {rd['age_days']} days old, exceeding the {s.max_model_age_days}-day freshness policy")
        elif name == "compare_mlflow_runs":
            for run in d["runs"]:
                facts.append(f"Run #{run['run_number']} ({run['run_id'][:8]}): {d['metric']}={_fmt(run['metrics'].get(d['metric']))}, "
                             f"params={run['params']}, quality_gate={run['quality_gate']}")
            if d["recommended"]:
                facts.append(f"Candidate by {d['metric']}: {d['recommended'][:8]} (params that differ: {d['params_that_differ']}).")
                unc.append("The recommendation uses the recorded metric only. Readiness of the candidate: " + d["readiness"]["verdict"] + ".")
        elif name == "list_mlflow_runs":
            for run in d["runs"]:
                facts.append(f"Run #{run['run_number']} {run['run_id'][:8]} [{run['status']}] metrics={ {k: round(v, 4) for k, v in run['metrics'].items()} } gate={run['quality_gate']}")
        elif name == "list_mlflow_experiments":
            facts.append("Experiments: " + ", ".join(e["name"] for e in d["experiments"]))
        elif name == "get_mlflow_run_details":
            facts.append(f"Run {d['run_id'][:8]}: params={d['params']}, metrics={d['metrics']}, artifacts={[a['path'] for a in d['artifacts']]}")
        elif name == "get_model_registry_info":
            facts.append(f"Registered model '{d['model_name']}': " + ("not registered" if not d["registered"] else f"{len(d['versions'])} version(s): {d['versions']}"))
        elif name == "get_experiment_dataset":
            if d["provenance_recorded"]:
                v = d.get("recorded_dataset_version") or "n/a"
                facts.append(f"Run {d['run_id'][:8]} (#{d.get('run_number')}) trained on dataset {d['dvc_file']} md5 {d['dvc_md5']}, version tag '{v}', "
                             f"recorded git commit {(d.get('recorded_git_commit') or 'n/a')[:8]}.")
                if d.get("verified_in_git"):
                    facts.append(f"That md5 was independently found in git history at commit {d['matching_commit'][:8]} (tags: {d['matching_tags'] or 'none'}).")
                else:
                    unc.append("The recorded md5 could not be matched to any commit of the .dvc file (verified_in_git=" f"{d.get('verified_in_git')}).")
            else:
                unc.append(f"Run {d['run_id'][:8]} recorded no DVC provenance, so its dataset version cannot be stated.")
        elif name == "get_dataset_version":
            facts.append(f"Current dataset {d['dvc_file']}: md5 {d['md5']}, {d['size']} bytes, last changed in commit {d['git_commit_short']} "
                         f"('{d['commit_message']}'), version tags {d['version_tags'] or 'none'}.")
        elif name == "list_dvc_datasets":
            facts.append("DVC-tracked: " + ", ".join(f"{x['dvc_file']} ({x.get('md5')})" for x in d["datasets"]))
        elif name == "compare_dataset_versions":
            facts.append(f"{d['a']['rev']} md5 {d['a']['md5']} vs {d['b']['rev']} md5 {d['b']['md5']}: content "
                         f"{'DIFFERS' if d['content_changed'] else 'identical'}; size delta {d['size_delta_bytes']} bytes.")
        elif name == "detect_data_changes":
            facts.append(f"DVC status: {d['summary']}" + (f" - {d['details']}" if d["changed"] else "."))
            if d["changed"]:
                acts.append("Tracked data was modified outside DVC: restore it with `dvc checkout` or record it as a new version (`dvc add` + git commit).")
        elif name == "explain_dataset_reproduction":
            facts.append(f"Version {d['rev']} = md5 {d['expected_md5']} (commit {d['git_commit'][:8]}).")
            acts += d["steps"]
        elif name == "checkout_dataset_version":
            facts.append(f"Dataset restored to {d['restored_rev']} (expected md5 {d['expected_md5']}).")
        elif name == "get_deployed_version":
            for dep in d["deployments"]:
                facts.append(f"Deployment {dep['deployment']}: images {dep['images']}, version label {dep['app_version_label']}, "
                             f"{dep['ready']}/{dep['desired']} replicas ready.")
                if "latest(implicit)" in dep["image_tags"] or any(t == "latest" for t in dep["image_tags"]):
                    unc.append("Image uses an implicit/`latest` tag, so the exact deployed build is not pinned.")
        elif name == "list_k8s_workloads":
            for dep in d["deployments"]:
                facts.append(f"Deployment {dep['name']}: desired {dep['desired']}, ready {dep['ready']}, available {dep['available']}.")
            facts.append("Pods: " + ", ".join(f"{p['name']}[{p['phase']}, ready={p['ready']}, restarts={p['restarts']}]" for p in d["pods"]))
        elif name == "find_unhealthy_pods":
            if d["unhealthy"]:
                for p in d["unhealthy"]:
                    facts.append(f"UNHEALTHY pod {p['pod']} ({p['phase']}, {p['restarts']} restarts): {'; '.join(p['reasons'])}.")
                    for e in p["warning_events"][-3:]:
                        facts.append(f"  event {e['reason']}: {e['message']}")
                acts.append("Inspect the pod logs/events above; fix the root cause (image, config, resources or probe) before restarting.")
            else:
                facts.append(f"All {d['total_pods']} pods are healthy (no crash loops, pull errors or unready containers).")
        elif name == "get_pod_status_and_logs":
            if d["problems"]:
                facts.append(f"Diagnosis for {d['pod']['name']}: {'; '.join(d['problems'])}.")
            if d["log_tail"]:
                facts.append("Last log lines: " + " | ".join(d["log_tail"][-5:]))
            elif d["note"]:
                unc.append(d["note"])
        elif name == "inspect_k8s_services":
            for sv in d["services"]:
                facts.append(f"Service {sv['name']} ({sv['type']}) selector={sv['selector']} ports={sv['ports']} ready endpoints={sv['ready_endpoints']}.")
                if sv["ready_endpoints"] == 0:
                    facts.append(f"Service {sv['name']} has NO ready endpoints -> requests cannot be served.")
        elif name in ("restart_k8s_deployment", "scale_k8s_deployment", "trigger_training_dag"):
            facts.append(f"{name} executed: {d}")
        elif name == "list_airflow_dags":
            facts.append("DAGs: " + ", ".join(f"{x['dag_id']}{' (approved)' if x['approved_for_trigger'] else ''}" for x in d["dags"]))
        elif name == "get_dag_run_status":
            if not d["runs"]:
                unc.append(f"Airflow DAG '{d['dag_id']}' has no recorded runs.")
            for run in d["runs"]:
                facts.append(f"Airflow run {run['dag_run_id']}: {run['state']}; tasks {run['tasks']}.")
            if d["runs"] and d["runs"][0]["state"] == "failed":
                retrain_evidence.append("the latest Airflow training run failed")
                if any("quality" in t and st == "failed" for t, st in d["runs"][0]["tasks"].items()):
                    retrain_evidence.append("the model quality gate failed in the latest pipeline run")
        elif name == "get_failed_tasks":
            if d["failed_tasks"]:
                facts.append(f"Failed tasks in {d['dag_run_id']}: {d['failed_tasks']}; root failure(s): {d['root_failures']}.")
            else:
                facts.append(f"No failed tasks in run {d['dag_run_id']} (state {d['run_state']}).")
        elif name == "get_task_diagnostics":
            facts.append(f"Task {d['task_id']} {d['state']} on attempt {d['try_number']} (retries configured: {d['max_tries']}); error lines: {d['error_lines'][-5:]}.")
            acts.append(f"Fix the cause shown in the {d['task_id']} log, then re-run the pipeline (needs your confirmation).")
    # cross-tool correlation
    best, ds_now, ds_run = _get(ctx, "find_best_mlflow_run"), _get(ctx, "get_dataset_version"), _get(ctx, "get_experiment_dataset")
    if best and best.ok and ds_now and ds_now.ok and ds_run and ds_run.ok and ds_run.data.get("provenance_recorded"):
        if ds_run.data["dvc_md5"] != ds_now.data["md5"]:
            facts.append("Current dataset md5 differs from the one the best model was trained on (data has moved on since training).")
            acts.append("Consider evaluating the model on the new data; the dataset change alone is not a policy trigger for retraining.")
        else:
            facts.append("The best model was trained on the CURRENT dataset version.")
    if plan["kind"] == "multi":
        if retrain_evidence:
            acts.insert(0, "Retraining IS justified by evidence: " + "; ".join(retrain_evidence) + ". Retraining was NOT started automatically - ask me to trigger it and confirm.")
        elif not (best and best.ok):
            acts.insert(0, "Cannot assess whether retraining is necessary: the MLflow evidence needed is unavailable. Retry once the service is reachable.")
        else:
            acts.insert(0, "Retraining is NOT currently necessary: no freshness-policy breach, failed quality gate or supplied degradation results were found in the evidence.")
        unc.append("Live production performance/drift data was not provided, so degradation in production cannot be ruled out.")
    return facts, unc, acts
