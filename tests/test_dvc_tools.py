from __future__ import annotations

import pytest

from agent.schemas import NoArgs
from agent.tools.base import ToolDenied, ToolRegistry
from agent.tools.dvc_tools import CommandRunner, DvcTools
from tests.conftest import FakeDvcRunner
from tests.fakes import FakeMlflow, mlflow_run


@pytest.fixture
def dvc(git_repo):
    return DvcTools(git_repo, ["data/dataset.csv.dvc"], FakeDvcRunner(git_repo), lambda: FakeMlflow([mlflow_run("run-v1", 1, 0.8, md5="1" * 32),
                                                                                                    mlflow_run("run-v2", 2, 0.9, md5="2" * 32),
                                                                                                    mlflow_run("run-x", 3, 0.9, md5="f" * 32),
                                                                                                    mlflow_run("run-none", 4, 0.9, md5=None)]), "exp")


def reg_for(dvc: DvcTools) -> ToolRegistry:
    r = ToolRegistry(5)
    r.register(dvc.tools())
    return r


def test_lists_tracked_datasets(dvc):
    d = reg_for(dvc).execute("list_dvc_datasets", {}).data
    assert d["count"] == 1 and d["datasets"][0]["md5"] == "2" * 32


def test_current_version_has_md5_commit_and_tag(dvc):
    r = reg_for(dvc).execute("get_dataset_version", {})
    assert r.ok and r.data["md5"] == "2" * 32 and "v2.0" in r.data["version_tags"] and len(r.data["git_commit"]) == 40


def test_compare_versions_detects_change(dvc):
    d = reg_for(dvc).execute("compare_dataset_versions", {"rev_a": "v1.0", "rev_b": "v2.0"}).data
    assert d["content_changed"] and d["size_delta_bytes"] == 150


def test_detect_changes_reports_modified_data(git_repo):
    status = '{"data/dataset.csv.dvc": [{"changed outs": {"data/dataset.csv": "modified"}}]}'
    d = DvcTools(git_repo, ["data/dataset.csv.dvc"], FakeDvcRunner(git_repo, status)).detect_changes(NoArgs())
    assert d["changed"] is True
    clean = DvcTools(git_repo, ["data/dataset.csv.dvc"], FakeDvcRunner(git_repo)).detect_changes(NoArgs())
    assert clean["changed"] is False


def test_reproduce_explanation_uses_real_revision(dvc):
    d = reg_for(dvc).execute("explain_dataset_reproduction", {"rev": "v1.0"}).data
    assert d["expected_md5"] == "1" * 32 and any("git checkout v1.0" in s for s in d["steps"])
    assert reg_for(dvc).execute("explain_dataset_reproduction", {"rev": "v9.9"}).status == "error"


def test_experiment_provenance_is_verified_against_git(dvc):
    reg = reg_for(dvc)
    ok = reg.execute("get_experiment_dataset", {"run_id": "run-v1"}).data
    assert ok["verified_in_git"] and "v1.0" in ok["matching_tags"]
    by_no = reg.execute("get_experiment_dataset", {"run_number": 2}).data
    assert by_no["dvc_md5"] == "2" * 32
    unknown = reg.execute("get_experiment_dataset", {"run_id": "run-x"}).data
    assert unknown["verified_in_git"] is False  # md5 not in history -> not claimed as verified
    none = reg.execute("get_experiment_dataset", {"run_id": "run-none"}).data
    assert none["provenance_recorded"] is False


def test_command_allowlist_blocks_unapproved_and_injection(git_repo):
    runner = CommandRunner(git_repo)
    for bad in (["git", "push"], ["rm", "-rf", "x"], ["dvc", "gc"], ["git", "log", "HEAD; rm -rf /"], ["git", "checkout", "-f"]):
        try:
            runner.run(bad)
        except ToolDenied:
            continue
        # `git checkout -f` is an approved subcommand; it must still be blocked by confirmation/rev validation at tool level
        assert bad[:2] == ["git", "checkout"]
    d = DvcTools(git_repo, ["data/dataset.csv.dvc"], runner)
    r = ToolRegistry(5)
    r.register(d.tools())
    assert r.execute("explain_dataset_reproduction", {"rev": "--hard;ls"}).status in ("denied", "error")


def test_checkout_requires_confirmation_then_restores(git_repo, dvc):
    reg = reg_for(dvc)
    assert reg.execute("checkout_dataset_version", {"rev": "v1.0"}).status == "needs_confirmation"
    r = reg.execute("checkout_dataset_version", {"rev": "v1.0"}, confirmed=True)
    assert r.ok and (git_repo / "data" / "dataset.csv.dvc").read_text().count("1" * 32) == 1
