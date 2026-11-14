"""Airflow 3 training DAG:
validate_dataset -> preprocess_data -> train_model -> evaluate_and_log_mlflow -> quality_gate -> promote_model

* retries=2 with a delay on every task (validation failures are non-retryable: AirflowFailException)
* a failed validation or failed quality gate fails the task, so `promote_model` (trigger_rule=all_success) never runs
* trigger with conf, e.g. {"inject_failure": "validation"} or {"degrade_model": true} to demo failure diagnosis
"""
from __future__ import annotations

import logging
import sys
from datetime import timedelta

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import dag, get_current_context, task

sys.path.insert(0, "/opt/airflow/project")  # repo is mounted here by infra/docker-compose.yaml

log = logging.getLogger("airflow.task")

DEFAULT_ARGS = {"owner": "mlops", "retries": 2, "retry_delay": timedelta(seconds=20)}


@dag(dag_id="training_pipeline", schedule=None, start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), catchup=False,
     default_args=DEFAULT_ARGS, tags=["mlops", "training"], max_active_runs=1,
     doc_md=__doc__)
def training_pipeline():
    @task
    def validate_dataset() -> dict:
        from training.pipeline import DataValidationError, validate
        conf = get_current_context()["dag_run"].conf or {}
        try:
            if conf.get("inject_failure") == "validation":
                raise DataValidationError("injected failure: validation stage")
            return validate()
        except DataValidationError as exc:
            log.error("ERROR dataset validation failed: %s", exc)
            raise AirflowFailException(str(exc)) from exc  # no point retrying bad data

    @task
    def preprocess_data(_validated: dict) -> dict:
        from training.pipeline import preprocess
        return preprocess()

    @task
    def train_model(paths: dict) -> dict:
        from training.pipeline import train
        conf = get_current_context()["dag_run"].conf or {}
        params = {k: v for k, v in conf.items() if k in ("n_estimators", "max_depth", "degrade_model")}
        return {"paths": paths, "model": train(paths, params)}

    @task
    def evaluate_and_log_mlflow(trained: dict) -> dict:
        from training.pipeline import evaluate_and_log
        return evaluate_and_log(trained["paths"], trained["model"], dag_run_id=get_current_context()["dag_run"].run_id)

    @task
    def quality_gate(result: dict) -> dict:
        from training.pipeline import QualityGateError
        from training.pipeline import quality_gate as gate
        try:
            return gate(result)
        except QualityGateError as exc:
            log.error("ERROR quality gate failed: %s", exc)
            raise AirflowFailException(str(exc)) from exc

    @task(trigger_rule="all_success")
    def promote_model(result: dict) -> dict:
        from training.pipeline import promote
        return promote(result)

    promote_model(quality_gate(evaluate_and_log_mlflow(train_model(preprocess_data(validate_dataset())))))


training_pipeline()
