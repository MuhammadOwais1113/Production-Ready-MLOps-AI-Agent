from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.config import Settings
from agent.orchestrator import Orchestrator, build_registry
from agent.tools.airflow_tools import AirflowClient
from agent.tools.dvc_tools import CommandRunner
from tests.fakes import FakeAirflowSession, FakeApps, FakeCore, FakeMlflow, k8s_factory, k8s_pod, mlflow_run


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def dvc_text(md5: str, size: int) -> str:
    return f"outs:\n- md5: {md5}\n  size: {size}\n  hash: md5\n  path: dataset.csv\n"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Real git repo with two dataset versions (v1.0, v2.0) recorded as .dvc pointers."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "data").mkdir()
    for tag, md5, size in (("v1.0", "1" * 32, 100), ("v2.0", "2" * 32, 250)):
        (tmp_path / "data" / "dataset.csv.dvc").write_text(dvc_text(md5, size))
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", f"dataset {tag}")
        git(tmp_path, "tag", tag)
    return tmp_path


class FakeDvcRunner(CommandRunner):
    """Real git (allow-listed) but a scripted `dvc` so CI does not need the DVC binary."""

    def __init__(self, cwd: Path, dvc_status_json: str = "{}") -> None:
        super().__init__(cwd)
        self.dvc_status_json = dvc_status_json

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        if cmd[0] == "dvc" and cmd[1] in ("status", "checkout"):
            from agent.tools.dvc_tools import ALLOWED_COMMANDS
            assert (cmd[0], cmd[1]) in ALLOWED_COMMANDS
            return 0, self.dvc_status_json if cmd[1] == "status" else "M data/dataset.csv", ""
        return super().run(cmd)


@pytest.fixture
def settings(git_repo: Path) -> Settings:
    return Settings(repo_root=git_repo, anthropic_api_key=None, airflow_password="pw", tool_timeout_s=3.0)


@pytest.fixture
def make_agent(settings: Settings, git_repo: Path):
    def _make(*, runs=None, mlflow_fail=None, pods=None, core=None, apps=None, airflow="ok", dvc_status="{}", registered=None, k8s_down=False):
        mlf = FakeMlflow(runs if runs is not None else [mlflow_run("r1" + "0" * 30, 1, 0.80), mlflow_run("r2" + "0" * 30, 2, 0.91, md5="2" * 32, age_days=2)],
                         fail=mlflow_fail, registered=registered)
        core = core or FakeCore(pods or [k8s_pod("inference-api-a"), k8s_pod("inference-api-b")])
        apps = apps or FakeApps()
        sess = FakeAirflowSession(airflow)
        def down():
            from agent.tools.base import ToolUnavailable
            raise ToolUnavailable("Kubernetes API not configured/reachable: connection refused")
        reg = build_registry(settings, k8s_api_factory=down if k8s_down else k8s_factory(core, apps),
                             airflow_client=AirflowClient("http://af", "admin", "pw", session=sess),
                             mlflow_factory=lambda: mlf, dvc_runner=FakeDvcRunner(git_repo, dvc_status))
        agent = Orchestrator(reg, settings)
        agent.fakes = {"core": core, "apps": apps, "airflow": sess, "mlflow": mlf}  # type: ignore[attr-defined]
        return agent
    return _make
