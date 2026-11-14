# Architecture

```
User / Engineer ──► FastAPI interface (api/main.py)   validation, request-id, response schema
                          │
                          ▼
                MLOps AI Agent (agent/orchestrator.py)
        LLM tool-calling (Claude)  ─or─  deterministic planner      permissions · tracing
                          │  every call goes through ToolRegistry (agent/tools/base.py):
                          │  pydantic validation · confirmation gate · timeout · error classes · JSON logs
      ┌───────────────┬───┴───────────┬────────────────┐
      ▼               ▼               ▼                ▼
   DVC tools      MLflow tools    Airflow tools    Kubernetes tools
   git + dvc      MlflowClient    Airflow 3 REST   kubernetes client
   (allow-list)   (tracking +     (JWT, /api/v2)   (approved namespaces)
                   registry)
      └───────────────┴───────┬───────┴────────────────┘
                              ▼
          Evidence-based response: known facts · uncertainties · recommended actions
```

## Components

| Path | Role |
|---|---|
| `agent/tools/base.py` | `Tool`, `ToolRegistry`: argument validation (pydantic), **confirmation gate for state-changing tools**, per-call timeout, typed errors (`ok / invalid / timeout / unavailable / denied / error / needs_confirmation`), structured logs |
| `agent/tools/dvc_tools.py` | Task 1. 7 tools. Subprocess runner restricted to an **allow-list** of `git`/`dvc` sub-commands, never a shell. |
| `agent/tools/mlflow_tools.py` | Task 2. 7 tools using `MlflowClient`. Best-run selection returns *readiness checks*; the highest metric is never declared production-ready. |
| `agent/tools/airflow_tools.py` | Task 3. 5 tools; JWT from `POST /auth/token`, then `/api/v2/...`. Triggering limited to **approved DAGs** and needs confirmation. |
| `agent/tools/kubernetes_tools.py` | Task 4. 7 tools. Every call limited to **approved namespaces**; restart/scale need confirmation; scale has a ceiling. |
| `agent/orchestrator.py` | Planner (LLM or offline), evidence correlation, report composer |
| `dags/training_pipeline.py`, `training/` | Airflow 3 DAG: validate → preprocess → train → evaluate/MLflow → quality gate → promote |
| `serving/app.py`, `kubernetes/` | Inference service + Deployment (2 replicas), ClusterIP Service, probes, requests/limits, ConfigMap, RBAC |
| `infra/docker-compose.yaml` | MLflow server + Airflow 3 for local use |

## Evidence chain for the mandatory scenario

1. `find_best_mlflow_run` → run id, metrics, readiness checks (gate tag, freshness, provenance, registry).
2. `get_experiment_dataset` → reads `dvc.md5`, `dvc.version`, `git.commit` tags written by the training pipeline, then **re-verifies** the md5 against the git history of the `.dvc` pointer.
3. `get_dataset_version` + `detect_data_changes` → has the data moved on since training?
4. `get_deployed_version`, `find_unhealthy_pods` → Kubernetes rollout and health.
5. `get_dag_run_status`, `get_failed_tasks` → recent pipeline results.
6. Correlation: retraining is recommended **only** for a freshness-policy breach (`MAX_MODEL_AGE_DAYS`), a failed quality gate (MLflow tag or Airflow task), or supplied degradation results. `trigger_training_dag` is never called from this request.

## Safety model

* **Confirmation is a property of the human request** (`confirm=true`), not of the model: in LLM mode the confirmation flag is injected by the orchestrator, so the model cannot grant it to itself.
* **Namespace allow-list** (`APPROVED_NAMESPACES`) on every Kubernetes call, reads included; RBAC Role in `kubernetes/agent-rbac.yaml` mirrors it.
* **Command allow-list** and revision sanitising for DVC/git.
* **No invented results**: a non-`ok` tool becomes an *uncertainty*; the system prompt forbids answering from memory, and the orchestrator appends tool-availability notes automatically.
* **Secrets** only from environment variables; logs redact keys such as `token`, `password`, `api_key` and bearer/JWT/`sk-` values.

## Provenance design

`training/pipeline.py::evaluate_and_log` tags each MLflow run with `dvc.md5`, `dvc.file`, `dvc.version` (git tag of the dataset commit), `git.commit` and `run_number`. This is the "recorded provenance" the DVC tools read, so *“Which dataset version was used for experiment N?”* is answered from evidence rather than inferred.

## Known limitations / improvements

* Offline planner is rule-based; LLM mode gives better paraphrase coverage but needs an API key.
* Airflow and Kubernetes are exercised in CI through mocks; live behaviour requires `infra/docker-compose.yaml` and a cluster (`kind`/Docker Desktop Kubernetes).
* Single-user confirmation flag; a production version would use signed approvals and per-user RBAC.
* Metrics are on one hold-out split; a real promotion policy would add drift and slice metrics.
