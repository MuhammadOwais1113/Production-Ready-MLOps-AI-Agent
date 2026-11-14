"""Task 3 - Apache Airflow 3 workflow tools (REST API v2, JWT bearer auth from POST /auth/token)."""
from __future__ import annotations

from typing import Any

import requests

from agent.schemas import DagRunArgs, NoArgs, TaskLogArgs, TriggerArgs
from agent.tools.base import Tool, ToolDenied, ToolError, ToolUnavailable


class AirflowClient:
    def __init__(self, base_url: str, username: str, password: str, session: Any | None = None,
                 timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self._user, self._pw = username, password
        self.s = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None

    def _login(self) -> None:
        if not self._pw:
            raise ToolUnavailable("AIRFLOW_PASSWORD is not configured")
        try:
            r = self.s.post(f"{self.base}/auth/token", json={"username": self._user, "password": self._pw},
                            timeout=self.timeout)
        except requests.RequestException as exc:
            raise ToolUnavailable(f"Airflow unreachable at {self.base}: {type(exc).__name__}") from exc
        if r.status_code not in (200, 201):
            raise ToolError(f"Airflow authentication failed (HTTP {r.status_code})")
        self._token = r.json().get("access_token")

    def request(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in (0, 1):
            if not self._token:
                self._login()
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            try:
                r = self.s.request(method, f"{self.base}/api/v2{path}", headers=headers, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                raise ToolUnavailable(f"Airflow unreachable at {self.base}: {type(exc).__name__}") from exc
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 404:
                raise ToolError(f"Airflow: not found ({path})")
            if r.status_code >= 500:
                raise ToolUnavailable(f"Airflow server error HTTP {r.status_code}")
            if r.status_code >= 400:
                raise ToolError(f"Airflow rejected request HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()
            except ValueError:
                return {"content": r.text}
        raise ToolError("Airflow authentication failed")


def _run(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("dag_run_id", "state", "run_type", "logical_date", "start_date", "end_date", "conf")}


class AirflowTools:
    def __init__(self, client: AirflowClient, approved_dags: list[str]) -> None:
        self.c = client
        self.approved = approved_dags

    def _latest_run_id(self, dag_id: str) -> str:
        runs = self.c.request("GET", f"/dags/{dag_id}/dagRuns", params={"order_by": "-start_date", "limit": 1})
        lst = runs.get("dag_runs", [])
        if not lst:
            raise ToolError(f"DAG '{dag_id}' has no runs")
        return lst[0]["dag_run_id"]

    def list_dags(self, _: NoArgs) -> dict[str, Any]:
        d = self.c.request("GET", "/dags", params={"limit": 100})
        return {"dags": [{"dag_id": x["dag_id"], "is_paused": x.get("is_paused"), "approved_for_trigger": x["dag_id"] in self.approved,
                          "last_parsed": x.get("last_parsed_time")} for x in d.get("dags", [])],
                "total": d.get("total_entries")}

    def run_status(self, a: DagRunArgs) -> dict[str, Any]:
        if a.dag_run_id:
            runs = [self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{a.dag_run_id}")]
        else:
            runs = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns",
                                  params={"order_by": "-start_date", "limit": a.limit}).get("dag_runs", [])
        out = []
        for r in runs:
            tis = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{r['dag_run_id']}/taskInstances").get("task_instances", [])
            out.append({**_run(r), "tasks": {t["task_id"]: t["state"] for t in tis}})
        return {"dag_id": a.dag_id, "runs": out}

    def trigger(self, a: TriggerArgs) -> dict[str, Any]:
        if a.dag_id not in self.approved:
            raise ToolDenied(f"DAG '{a.dag_id}' is not on the approved-trigger list {self.approved}")
        r = self.c.request("POST", f"/dags/{a.dag_id}/dagRuns", json={"logical_date": None, "conf": a.conf})
        return {"triggered": True, **_run(r)}

    def failed_tasks(self, a: DagRunArgs) -> dict[str, Any]:
        rid = a.dag_run_id or self._latest_run_id(a.dag_id)
        run = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{rid}")
        tis = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{rid}/taskInstances").get("task_instances", [])
        bad = [{"task_id": t["task_id"], "state": t["state"], "try_number": t.get("try_number"), "max_tries": t.get("max_tries")}
               for t in tis if t["state"] in ("failed", "upstream_failed")]
        return {"dag_run_id": rid, "run_state": run.get("state"), "failed_tasks": bad,
                "root_failures": [b["task_id"] for b in bad if b["state"] == "failed"]}

    def task_diagnostics(self, a: TaskLogArgs) -> dict[str, Any]:
        rid = a.dag_run_id or self._latest_run_id(a.dag_id)
        tis = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{rid}/taskInstances").get("task_instances", [])
        task = next((t for t in tis if t["task_id"] == a.task_id), None) if a.task_id else \
            next((t for t in tis if t["state"] == "failed"), None)
        if task is None:
            raise ToolError("No matching (failed) task instance found" if not a.task_id else f"Task '{a.task_id}' not found")
        try_no = task.get("try_number") or 1
        log = self.c.request("GET", f"/dags/{a.dag_id}/dagRuns/{rid}/taskInstances/{task['task_id']}/logs/{try_no}",
                             params={"full_content": "true"})
        content = log.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(x.get("event", x)) if isinstance(x, dict) else str(x) for x in content)
        lines = content.splitlines()
        errors = [ln for ln in lines if any(k in ln for k in ("ERROR", "Error", "Exception", "Traceback", "failed"))]
        return {"dag_run_id": rid, "task_id": task["task_id"], "state": task["state"], "try_number": try_no,
                "max_tries": task.get("max_tries"), "start_date": task.get("start_date"), "end_date": task.get("end_date"),
                "duration": task.get("duration"), "error_lines": errors[-15:], "log_tail": lines[-30:]}

    def tools(self) -> list[Tool]:
        return [
            Tool("list_airflow_dags", "List Airflow DAGs and whether each is approved for triggering.", NoArgs, self.list_dags, "airflow"),
            Tool("get_dag_run_status", "Get recent DAG runs (or one run) with per-task states.", DagRunArgs, self.run_status, "airflow"),
            Tool("trigger_training_dag", "Trigger an approved training DAG run.", TriggerArgs, self.trigger, "airflow", mutating=True),
            Tool("get_failed_tasks", "Identify failed / upstream_failed tasks of a DAG run (default: latest).", DagRunArgs, self.failed_tasks, "airflow"),
            Tool("get_task_diagnostics", "Get task execution info and failure log excerpts for a task (default: first failed task of latest run).", TaskLogArgs, self.task_diagnostics, "airflow"),
        ]
