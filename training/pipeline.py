"""Pipeline stages: validate -> preprocess -> train -> evaluate/log (MLflow) -> quality gate -> promote.

Pure Python (no Airflow imports) so the same code runs in the Airflow DAG, from the CLI (training/train.py)
and in unit tests. Failures raise typed exceptions; Airflow turns them into task failures."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import yaml
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("training.pipeline")

REPO = Path(os.getenv("REPO_ROOT", Path(__file__).resolve().parents[1]))
DATA_FILE = "data/dataset.csv"
DVC_FILE = "data/dataset.csv.dvc"
WORK = Path(os.getenv("PIPELINE_WORKDIR", REPO / "artifacts"))
EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT", "mlops-agent-demo")
MODEL_NAME = os.getenv("MLFLOW_MODEL_NAME", "classifier")
GATE_F1 = float(os.getenv("QUALITY_GATE_F1", "0.85"))
FEATURES = [f"f{i}" for i in range(8)]


class DataValidationError(Exception):
    pass


class QualityGateError(Exception):
    pass


def validate(data_path: str | Path | None = None, min_rows: int = 200) -> dict[str, Any]:
    path = Path(data_path or REPO / DATA_FILE)
    if not path.exists():
        raise DataValidationError(f"dataset not found: {path} (run `dvc checkout`/`dvc pull`)")
    df = pd.read_csv(path)
    missing = [c for c in FEATURES + ["label"] if c not in df.columns]
    if missing:
        raise DataValidationError(f"missing columns: {missing}")
    if len(df) < min_rows:
        raise DataValidationError(f"too few rows: {len(df)} < {min_rows}")
    nulls = df[FEATURES + ["label"]].isna().sum()
    if nulls.sum() > 0:
        bad = {k: int(v) for k, v in nulls.items() if v}
        raise DataValidationError(f"null values found: {bad}")
    if df["label"].nunique() < 2:
        raise DataValidationError("label has a single class")
    info = {"rows": len(df), "columns": len(df.columns), "class_balance": df["label"].value_counts(normalize=True).round(3).to_dict()}
    log.info("validation passed: %s", info)
    return info


def preprocess(data_path: str | Path | None = None, workdir: Path | None = None, seed: int = 42) -> dict[str, str]:
    wd = Path(workdir or WORK)
    wd.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(data_path or REPO / DATA_FILE)
    tr, te = train_test_split(df, test_size=0.25, random_state=seed, stratify=df["label"])
    sc = StandardScaler().fit(tr[FEATURES])
    for name, part in (("train", tr), ("test", te)):
        out = part.copy()
        out[FEATURES] = sc.transform(part[FEATURES])
        out.to_csv(wd / f"{name}.csv", index=False)
    joblib.dump(sc, wd / "scaler.joblib")
    log.info("preprocessed: train=%d test=%d", len(tr), len(te))
    return {"train": str(wd / "train.csv"), "test": str(wd / "test.csv"), "scaler": str(wd / "scaler.joblib")}


def train(paths: dict[str, str], params: dict[str, Any] | None = None, workdir: Path | None = None) -> dict[str, Any]:
    p = {"n_estimators": 100, "max_depth": 8, "random_state": 42, **(params or {})}
    if p.pop("degrade_model", False):  # injected failure: deliberately weak model
        p.update(n_estimators=1, max_depth=1)
    wd = Path(workdir or WORK)
    df = pd.read_csv(paths["train"])
    model = RandomForestClassifier(**p).fit(df[FEATURES], df["label"])
    joblib.dump(model, wd / "model.joblib")
    log.info("trained model with params %s", p)
    return {"model": str(wd / "model.joblib"), "params": p}


def dvc_provenance(repo: Path | None = None) -> dict[str, str]:
    """Read the dataset reference (DVC md5 + git commit/tag) that is recorded on every run."""
    repo = Path(repo or REPO)
    prov: dict[str, str] = {"dvc.file": DVC_FILE}
    try:
        meta = yaml.load((repo / DVC_FILE).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["outs"][0]
        prov["dvc.md5"], prov["dvc.size"] = str(meta["md5"]), str(meta.get("size"))
    except Exception as exc:  # noqa: BLE001
        log.warning("no DVC pointer readable: %s", exc)

    def git(*a: str) -> str:
        try:
            return subprocess.run(["git", "-c", "safe.directory=*", *a], cwd=repo, capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    last = git("log", "-1", "--format=%H", "--", DVC_FILE)
    prov["git.commit"] = last or os.getenv("GIT_COMMIT", "unknown")
    tags = git("tag", "--points-at", last) if last else ""
    prov["dvc.version"] = tags.splitlines()[0] if tags else os.getenv("DATASET_VERSION", "untagged")
    return prov


def evaluate_and_log(paths: dict[str, str], model_info: dict[str, Any], dag_run_id: str | None = None,
                     tracking_uri: str | None = None) -> dict[str, Any]:
    import mlflow
    from mlflow.tracking import MlflowClient
    uri = tracking_uri or os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{(REPO / 'mlflow.db').as_posix()}")
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(EXPERIMENT)
    client = MlflowClient(uri)
    exp = client.get_experiment_by_name(EXPERIMENT)
    run_number = len(client.search_runs([exp.experiment_id], max_results=10000)) + 1
    model = joblib.load(model_info["model"])
    te = pd.read_csv(paths["test"])
    pred = model.predict(te[FEATURES])
    metrics = {"accuracy": accuracy_score(te["label"], pred), "f1": f1_score(te["label"], pred),
               "precision": precision_score(te["label"], pred), "recall": recall_score(te["label"], pred)}
    with mlflow.start_run(run_name=f"rf-run-{run_number}") as run:
        mlflow.log_params(model_info["params"])
        mlflow.log_metrics(metrics)
        tags = {**dvc_provenance(), "run_number": str(run_number)}
        if dag_run_id:
            tags["airflow.dag_run_id"] = dag_run_id
        mlflow.set_tags(tags)
        mlflow.log_artifact(model_info["model"])
        mlflow.log_artifact(paths["scaler"])
        mlflow.log_dict({**metrics, **{k: v for k, v in tags.items()}}, "evaluation.json")
        rid = run.info.run_id
    log.info("logged MLflow run %s #%d metrics=%s", rid, run_number, {k: round(v, 4) for k, v in metrics.items()})
    return {"run_id": rid, "run_number": run_number, "metrics": metrics, "tracking_uri": uri}


def quality_gate(result: dict[str, Any], threshold: float | None = None) -> dict[str, Any]:
    """Record gate outcome on the run; raise to block downstream promotion if it fails."""
    from mlflow.tracking import MlflowClient
    thr = GATE_F1 if threshold is None else threshold
    f1 = result["metrics"]["f1"]
    passed = f1 >= thr
    MlflowClient(result["tracking_uri"]).set_tag(result["run_id"], "quality_gate", "passed" if passed else "failed")
    if not passed:
        raise QualityGateError(f"Quality gate FAILED: f1={f1:.4f} < threshold {thr}. Promotion blocked.")
    log.info("quality gate passed: f1=%.4f >= %s", f1, thr)
    return {**result, "gate": "passed"}


def promote(result: dict[str, Any]) -> dict[str, Any]:
    """Register the model version and move the 'candidate' alias (only reachable after the gate passes)."""
    from mlflow.tracking import MlflowClient
    c = MlflowClient(result["tracking_uri"])
    try:
        c.create_registered_model(MODEL_NAME)
    except Exception:  # noqa: BLE001 - already exists
        pass
    run = c.get_run(result["run_id"])
    mv = c.create_model_version(MODEL_NAME, source=f"{run.info.artifact_uri}/model.joblib", run_id=result["run_id"])
    c.set_registered_model_alias(MODEL_NAME, "candidate", mv.version)
    log.info("registered %s v%s", MODEL_NAME, mv.version)
    return {**result, "model_version": mv.version}


def run_all(params: dict[str, Any] | None = None, dag_run_id: str | None = None, tracking_uri: str | None = None) -> dict[str, Any]:
    """Convenience runner used by training/train.py (same stages as the Airflow DAG)."""
    params = dict(params or {})
    if params.pop("inject_failure", None) == "validation":
        raise DataValidationError("injected failure: validation stage")
    validate()
    paths = preprocess()
    info = train(paths, params)
    res = evaluate_and_log(paths, info, dag_run_id, tracking_uri)
    gated = quality_gate(res)
    return promote(gated)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_all(), indent=2, default=str))
