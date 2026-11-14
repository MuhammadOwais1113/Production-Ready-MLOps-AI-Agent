# Assignment 03 – Production-Ready MLOps AI Agent

An agent that answers MLOps questions through one conversational interface by calling **real tools** for
**DVC** (dataset versions), **MLflow** (experiments/registry), **Apache Airflow 3** (pipelines) and **Kubernetes** (deployment).
Operational claims always come from tool results; unavailable tools become explicit uncertainties. See [architecture.md](architecture.md).

## 1. Installation

```bash
python -m venv .venv && source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev,training,dvc]"
```

## 2. Environment configuration (no secrets in the repo)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Enables LLM tool-calling mode. Without it the deterministic planner is used. |
| `AGENT_MODEL` | `claude-sonnet-5-5` | Model for LLM mode |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | e.g. `sqlite:///mlflow.db` for the local demo |
| `MLFLOW_EXPERIMENT` / `MLFLOW_MODEL_NAME` | `mlops-agent-demo` / `classifier` | |
| `AIRFLOW_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD` | `http://localhost:8080`, `admin`, – | Airflow 3 JWT login |
| `APPROVED_DAGS` | `training_pipeline` | DAGs the agent may trigger |
| `APPROVED_NAMESPACES`, `K8S_NAMESPACE` | `mlops-agent` | Kubernetes namespace allow-list |
| `MAX_MODEL_AGE_DAYS`, `QUALITY_GATE_F1` | `30`, `0.85` | Freshness policy and quality gate |

Kubernetes access uses your kubeconfig (or the in-cluster service account from `kubernetes/agent-rbac.yaml`).

## 3. Launching each service

**Dataset (DVC) – done once**
```bash
python scripts/make_dataset.py --version 1
dvc init && dvc remote add -d localstore dvc_remote
dvc add data/dataset.csv && git add -A && git commit -m "dataset v1" && git tag v1.0 && dvc push
```
Create more versions with `--version 2`, `dvc add`, commit, `git tag v2.0`. Reproduce an old one: `git checkout v1.0 -- data/dataset.csv.dvc && dvc checkout`.

**Training without Airflow (writes MLflow runs with DVC provenance)**
```bash
export MLFLOW_TRACKING_URI=sqlite:///mlflow.db
python -m training.train                 # also: --degrade (fails quality gate), --inject-validation
```

**MLflow + Airflow 3**
```bash
cd infra && cp .env.example .env         # choose AIRFLOW_ADMIN_PASSWORD
docker compose up -d                     # MLflow :5000, Airflow :8080 (DAG: training_pipeline)
```
Trigger a failing run for demos with conf `{"inject_failure": "validation"}` or `{"degrade_model": true}`.

**Inference service on Kubernetes**
```bash
python scripts/prepare_model_dir.py && docker build -t mlops-inference:1.0.0 .
kind load docker-image mlops-inference:1.0.0      # or use Docker Desktop's Kubernetes
kubectl apply -f kubernetes/namespace.yaml -f kubernetes/configmap.yaml -f kubernetes/deployment.yaml -f kubernetes/service.yaml
kubectl -n mlops-agent get pods,svc
```

**The agent**
```bash
python -m agent.main                                   # interactive CLI
python -m agent.main --ask "Which dataset version was used for experiment 4?"
uvicorn api.main:app --port 9000                       # FastAPI interface
curl -s localhost:9000/chat -H 'content-type: application/json' \
     -d '{"message":"Why is my prediction API unavailable?"}'
```
State-changing requests (trigger DAG, restart/scale, dataset checkout) return `needs_confirmation` until you send `"confirm": true` (CLI: `--confirm` or prefix `!confirm `).

## 4. Tests and lint
```bash
ruff check . && pytest
```
59 tests; Kubernetes, Airflow, MLflow client failures, DVC CLI and the LLM are mocked, git and the MLflow SQLite store are real.
CI (`.github/workflows/ci.yml`) runs ruff + pytest on every pull request.

## 5. Agent interaction examples
* "Which dataset version was used for experiment 4?" → DVC + MLflow provenance, verified in git
* "Compare the last five training runs and recommend the best candidate based on F1 score."
* "Trigger the model training pipeline and report whether execution succeeded." (needs `confirm`)
* "Why is my prediction API unavailable?"
* "Find the best recent model, identify the dataset version used to train it, check the deployment status, and explain whether retraining is necessary."

Reproduce the evidence in `evidence/`: `python scripts/demo.py` (real DVC + MLflow; Kubernetes/Airflow mocked unless `--live`).
The technical report is `report/MLOps_Agent_Technical_Report.pdf` (built by `python scripts/build_report.py`).
