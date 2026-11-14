"""Task 2 - MLflow experiment-intelligence tools (MlflowClient only; no UI scraping).

Provenance tags written by training/train.py:
  dvc.md5, dvc.file, dvc.version, git.commit, run_number, quality_gate, airflow.dag_run_id
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from agent.schemas import (BestRunArgs, CompareExperimentsArgs, CompareRunsArgs, ListRunsArgs, NoArgs,
                           RegistryArgs, RunRefArgs)
from agent.tools.base import Tool, ToolError, ToolUnavailable


def make_client_factory(tracking_uri: str) -> Callable[[], Any]:
    def factory() -> Any:
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
        from mlflow.tracking import MlflowClient
        return MlflowClient(tracking_uri=tracking_uri)
    return factory


def _run_summary(run: Any) -> dict[str, Any]:
    t = run.data.tags
    return {"run_id": run.info.run_id, "run_name": t.get("mlflow.runName"), "run_number": t.get("run_number"),
            "status": run.info.status, "start_time": run.info.start_time,
            "metrics": dict(run.data.metrics), "params": dict(run.data.params),
            "quality_gate": t.get("quality_gate"), "dvc_md5": t.get("dvc.md5"), "dvc_version": t.get("dvc.version"),
            "git_commit": t.get("git.commit"), "airflow_dag_run_id": t.get("airflow.dag_run_id")}


class MlflowTools:
    def __init__(self, client_factory: Callable[[], Any], default_experiment: str, default_model: str,
                 gate_f1: float = 0.85, max_age_days: int = 30) -> None:
        self._factory = client_factory
        self.default_experiment = default_experiment
        self.default_model = default_model
        self.gate_f1 = gate_f1
        self.max_age_days = max_age_days

    # ---- plumbing --------------------------------------------------------
    def _client(self) -> Any:
        try:
            return self._factory()
        except Exception as exc:  # noqa: BLE001
            raise ToolUnavailable(f"Cannot create MLflow client: {exc}") from exc

    def _guard(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (ToolError, ToolUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            msg = str(exc)
            if "RESOURCE_DOES_NOT_EXIST" in msg or "does not exist" in msg.lower():
                raise ToolError(f"MLflow: {msg}") from exc
            raise ToolUnavailable(f"MLflow unreachable or failed ({name}): {msg[:300]}") from exc

    def _experiment_id(self, client: Any, name: str | None) -> str:
        exp = client.get_experiment_by_name(name or self.default_experiment)
        if exp is None:
            raise ToolError(f"MLflow experiment '{name or self.default_experiment}' does not exist")
        return exp.experiment_id

    def _recent(self, client: Any, name: str | None, limit: int) -> list[Any]:
        eid = self._experiment_id(client, name)
        return client.search_runs([eid], filter_string="attributes.status = 'FINISHED'", max_results=limit,
                                  order_by=["attributes.start_time DESC"])

    # ---- tools -----------------------------------------------------------
    def list_experiments(self, _: NoArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            exps = c.search_experiments()
            return {"experiments": [{"experiment_id": e.experiment_id, "name": e.name,
                                     "lifecycle_stage": e.lifecycle_stage} for e in exps]}
        return self._guard(go)

    def list_runs(self, a: ListRunsArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            eid = self._experiment_id(c, a.experiment_name)
            runs = c.search_runs([eid], max_results=a.limit, order_by=["attributes.start_time DESC"])
            return {"experiment": a.experiment_name or self.default_experiment, "runs": [_run_summary(r) for r in runs]}
        return self._guard(go)

    def run_details(self, a: RunRefArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            run = c.get_run(a.run_id) if a.run_id else (self._recent(c, None, 1) or [None])[0]
            if run is None:
                raise ToolError("No runs found")
            arts = [{"path": f.path, "is_dir": f.is_dir, "size": f.file_size}
                    for f in c.list_artifacts(run.info.run_id)]
            return {**_run_summary(run), "tags": {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")},
                    "artifacts": arts}
        return self._guard(go)

    def _readiness(self, s: dict[str, Any], metric: str, registered_run_ids: set[str]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        gate = s.get("quality_gate")
        checks.append({"check": "quality_gate_passed", "ok": gate == "passed", "detail": gate or "no quality_gate tag recorded"})
        f1 = s["metrics"].get("f1")
        if f1 is not None:
            checks.append({"check": f"f1>={self.gate_f1}", "ok": f1 >= self.gate_f1, "detail": f"f1={f1:.4f}"})
        checks.append({"check": "dataset_provenance_recorded", "ok": bool(s.get("dvc_md5")), "detail": s.get("dvc_md5") or "missing"})
        checks.append({"check": "registered_in_model_registry", "ok": s["run_id"] in registered_run_ids,
                       "detail": "registered" if s["run_id"] in registered_run_ids else "not registered"})
        age_days = (time.time() * 1000 - s["start_time"]) / 86_400_000
        checks.append({"check": f"fresh(<={self.max_age_days}d)", "ok": age_days <= self.max_age_days, "detail": f"{age_days:.1f} days old"})
        ok = all(c["ok"] for c in checks)
        return {"checks": checks, "age_days": round(age_days, 2),
                "verdict": "meets all recorded readiness checks - still needs human promotion review" if ok
                else "NOT demonstrated production-ready: " + ", ".join(c["check"] for c in checks if not c["ok"])}

    def _registered_run_ids(self, client: Any) -> set[str]:
        try:
            return {mv.run_id for mv in client.search_model_versions(f"name='{self.default_model}'")}
        except Exception:  # noqa: BLE001 - registry is optional
            return set()

    def best_run(self, a: BestRunArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            runs = [_run_summary(r) for r in self._recent(c, a.experiment_name, a.limit)]
            minimise = a.metric.startswith("-")
            m = a.metric.lstrip("-")
            scored = [r for r in runs if m in r["metrics"]]
            if not scored:
                raise ToolError(f"No finished runs with metric '{m}' among the {len(runs)} most recent runs")
            scored.sort(key=lambda r: r["metrics"][m], reverse=not minimise)
            best = scored[0]
            reg = self._registered_run_ids(c)
            margin = None
            if len(scored) > 1:
                margin = round(abs(best["metrics"][m] - scored[1]["metrics"][m]), 6)
            return {"metric": m, "direction": "min" if minimise else "max", "runs_considered": len(runs),
                    "best": best, "runner_up_margin": margin, "readiness": self._readiness(best, m, reg),
                    "caveat": "Highest metric does not imply production-readiness; see readiness checks."}
        return self._guard(go)

    def compare_runs(self, a: CompareRunsArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            if a.run_ids:
                runs = [_run_summary(c.get_run(r)) for r in a.run_ids]
            else:
                runs = [_run_summary(r) for r in self._recent(c, a.experiment_name, a.last_n or 5)]
            if len(runs) < 2:
                raise ToolError("Need at least two runs to compare")
            m = a.metric
            ranked = sorted([r for r in runs if m in r["metrics"]], key=lambda r: r["metrics"][m], reverse=True)
            reg = self._registered_run_ids(c)
            param_keys = sorted({k for r in runs for k in r["params"]})
            differing = [k for k in param_keys if len({r["params"].get(k) for r in runs}) > 1]
            rec = ranked[0] if ranked else None
            return {"metric": m, "runs": runs, "ranking": [r["run_id"] for r in ranked],
                    "params_that_differ": differing,
                    "recommended": rec["run_id"] if rec else None,
                    "readiness": self._readiness(rec, m, reg) if rec else None,
                    "caveat": "Recommendation is by the recorded metric only; check readiness before promotion."}
        return self._guard(go)

    def compare_experiments(self, a: CompareExperimentsArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            out = []
            for name in a.experiment_names:
                runs = [_run_summary(r) for r in self._recent(c, name, 50)]
                vals = [r["metrics"][a.metric] for r in runs if a.metric in r["metrics"]]
                out.append({"experiment": name, "runs": len(runs),
                            "best": max(vals) if vals else None,
                            "mean": round(sum(vals) / len(vals), 6) if vals else None})
            return {"metric": a.metric, "experiments": out}
        return self._guard(go)

    def registry_info(self, a: RegistryArgs) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            c = self._client()
            name = a.model_name or self.default_model
            try:
                rm = c.get_registered_model(name)
            except Exception as exc:  # noqa: BLE001
                if "RESOURCE_DOES_NOT_EXIST" in str(exc) or "not found" in str(exc).lower():
                    return {"model_name": name, "registered": False, "versions": []}
                raise
            versions = c.search_model_versions(f"name='{name}'")
            return {"model_name": name, "registered": True, "aliases": dict(getattr(rm, "aliases", {}) or {}),
                    "versions": [{"version": v.version, "run_id": v.run_id, "status": v.status,
                                  "stage": getattr(v, "current_stage", None), "aliases": list(getattr(v, "aliases", []) or [])}
                                 for v in versions]}
        return self._guard(go)

    def tools(self) -> list[Tool]:
        return [
            Tool("list_mlflow_experiments", "List MLflow experiments.", NoArgs, self.list_experiments, "mlflow"),
            Tool("list_mlflow_runs", "List the most recent training runs (metrics, params, provenance tags).", ListRunsArgs, self.list_runs, "mlflow"),
            Tool("get_mlflow_run_details", "Get metrics, parameters, tags and artifacts of one run (default: newest).", RunRefArgs, self.run_details, "mlflow"),
            Tool("find_best_mlflow_run", "Find the best recent run by a metric (default f1) with production-readiness checks.", BestRunArgs, self.best_run, "mlflow"),
            Tool("compare_mlflow_runs", "Compare runs side by side (by run ids or the last N) and recommend a candidate.", CompareRunsArgs, self.compare_runs, "mlflow"),
            Tool("compare_mlflow_experiments", "Compare two or more experiments by best/mean metric.", CompareExperimentsArgs, self.compare_experiments, "mlflow"),
            Tool("get_model_registry_info", "Get registered model versions, aliases and stages.", RegistryArgs, self.registry_info, "mlflow"),
        ]
