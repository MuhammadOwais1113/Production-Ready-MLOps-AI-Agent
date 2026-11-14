"""Task 1 - DVC data-management tools.

Commands are executed through an allow-listed subprocess runner: only a fixed set of
read-only git/dvc sub-commands (plus a confirmed `checkout`) can ever be invoked, and never via a shell."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from agent.schemas import CheckoutArgs, CompareDatasetArgs, DatasetArgs, ExperimentRunRef, NoArgs, ReproduceArgs
from agent.tools.base import Tool, ToolDenied, ToolError, ToolUnavailable

# (executable, subcommand) pairs that may be run. Anything else is rejected.
ALLOWED_COMMANDS: set[tuple[str, str]] = {
    ("git", "rev-parse"), ("git", "log"), ("git", "show"), ("git", "tag"), ("git", "checkout"),
    ("dvc", "status"), ("dvc", "checkout"), ("dvc", "diff"),
}
_FORBIDDEN_TOKENS = {";", "&&", "|", "`", "$(", ">", "<"}


def _safe_rev(rev: str) -> str:
    if not rev or rev.startswith("-") or any(c in rev for c in " ;|&$`<>\\\"'"):
        raise ToolDenied(f"Unsafe git revision: {rev!r}")
    return rev


class CommandRunner:
    """Runs allow-listed commands (no shell) inside the repo root."""

    def __init__(self, cwd: Path, timeout: float = 30.0) -> None:
        self.cwd, self.timeout = cwd, timeout

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        if len(cmd) < 2 or (cmd[0], cmd[1]) not in ALLOWED_COMMANDS:
            raise ToolDenied(f"Command not on the approved list: {' '.join(cmd[:2])}")
        if any(tok in part for part in cmd if not part.startswith("--format=") for tok in _FORBIDDEN_TOKENS):
            raise ToolDenied("Shell metacharacters are not permitted")
        try:
            p = subprocess.run(cmd, cwd=self.cwd, capture_output=True, text=True, timeout=self.timeout, shell=False)
        except FileNotFoundError as exc:
            raise ToolUnavailable(f"'{cmd[0]}' executable not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"{' '.join(cmd[:2])} timed out") from exc
        return p.returncode, p.stdout.strip(), p.stderr.strip()


class DvcTools:
    def __init__(self, repo_root: Path, data_files: list[str], runner: CommandRunner | None = None,
                 mlflow_client_factory: Callable[[], Any] | None = None, experiment: str = "") -> None:
        self.root = Path(repo_root)
        self.data_files = data_files
        self.runner = runner or CommandRunner(self.root)
        self._mlflow = mlflow_client_factory
        self.experiment = experiment

    # ---- helpers -------------------------------------------------------
    def _dvc_files(self) -> list[str]:
        found = {p.relative_to(self.root).as_posix() for p in self.root.glob("**/*.dvc")
                 if ".venv" not in p.parts and ".dvc" not in p.parts}
        return sorted(found | {f for f in self.data_files if (self.root / f).exists()})

    def _pick(self, dataset: str | None) -> str:
        files = self._dvc_files()
        if not files:
            raise ToolError("No DVC-tracked datasets (.dvc files) found in the repository")
        if dataset:
            ds = dataset if dataset.endswith(".dvc") else dataset + ".dvc"
            if ds not in files:
                raise ToolError(f"Dataset '{dataset}' is not tracked by DVC. Tracked: {files}")
            return ds
        return files[0]

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        # BaseLoader keeps every scalar a string: an md5 such as "12e4..." or "2222..." must never become a float/int
        doc = yaml.load(text, Loader=yaml.BaseLoader) or {}
        outs = doc.get("outs") or []
        if not outs:
            raise ToolError("Malformed .dvc file: no 'outs'")
        o = outs[0]
        size = int(o["size"]) if o.get("size") else None
        nfiles = int(o["nfiles"]) if o.get("nfiles") else None
        return {"md5": o.get("md5"), "size": size, "nfiles": nfiles, "path": o.get("path")}

    def _git(self, *args: str) -> str:
        rc, out, err = self.runner.run(["git", *args])
        if rc != 0:
            raise ToolError(f"git {args[0]} failed: {err or out}")
        return out

    def _dvc_at(self, rev: str, dvc_file: str) -> dict[str, Any]:
        _safe_rev(rev)
        return self._parse(self._git("show", f"{rev}:{dvc_file}"))

    def _commit_of(self, rev: str) -> dict[str, str]:
        _safe_rev(rev)
        out = self._git("log", "-1", "--format=%H|%h|%s|%ci", rev)
        full, short, subj, date = out.split("|", 3)
        return {"commit": full, "short": short, "subject": subj, "date": date}

    def _tags_at(self, rev: str) -> list[str]:
        out = self._git("tag", "--points-at", rev)
        return [t for t in out.splitlines() if t]

    # ---- tools ---------------------------------------------------------
    def list_datasets(self, _: NoArgs) -> dict[str, Any]:
        items = []
        for f in self._dvc_files():
            try:
                meta = self._parse((self.root / f).read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                meta = {"error": str(exc)}
            items.append({"dvc_file": f, **meta})
        remote = None
        cfg = self.root / ".dvc" / "config"
        if cfg.exists():
            remote = cfg.read_text(encoding="utf-8")
        return {"datasets": items, "count": len(items), "dvc_config": remote}

    def get_version(self, a: DatasetArgs) -> dict[str, Any]:
        f = self._pick(a.dataset)
        meta = self._parse((self.root / f).read_text(encoding="utf-8"))
        last = self._git("log", "-1", "--format=%H", "--", f)
        if not last:
            raise ToolError(f"{f} has no git history yet (not committed)")
        commit = self._commit_of(last)
        head = self._git("rev-parse", "HEAD")
        return {"dvc_file": f, **meta, "git_commit": commit["commit"], "git_commit_short": commit["short"],
                "commit_message": commit["subject"], "commit_date": commit["date"],
                "version_tags": self._tags_at(last), "head": head}

    def compare_versions(self, a: CompareDatasetArgs) -> dict[str, Any]:
        f = self._pick(a.dataset)
        va, vb = self._dvc_at(a.rev_a, f), self._dvc_at(a.rev_b, f)
        changed = va["md5"] != vb["md5"]
        return {"dvc_file": f, "a": {"rev": a.rev_a, **va, **self._commit_of(a.rev_a)},
                "b": {"rev": a.rev_b, **vb, **self._commit_of(a.rev_b)}, "content_changed": changed,
                "size_delta_bytes": (vb["size"] or 0) - (va["size"] or 0)}

    def detect_changes(self, _: NoArgs) -> dict[str, Any]:
        import json
        status: dict[str, Any] = {}
        for f in self._dvc_files():  # only data pointers; pipeline stages are not "data changes"
            rc, out, err = self.runner.run(["dvc", "status", f, "--json"])
            if rc != 0:
                raise ToolError(f"dvc status failed: {err or out}")
            status.update(json.loads(out) if out else {})
        return {"changed": bool(status), "details": status,
                "summary": "Tracked data differs from its recorded DVC hash" if status
                else "Data and pipelines are up to date"}

    def explain_reproduce(self, a: ReproduceArgs) -> dict[str, Any]:
        f = self._pick(a.dataset)
        meta = self._dvc_at(a.rev, f)  # also proves the revision actually exists
        commit = self._commit_of(a.rev)
        steps = [f"git checkout {a.rev} -- {f}    # restore the dataset pointer recorded at that revision",
                 f"dvc checkout {f}    # materialise the exact file from the DVC cache",
                 "dvc pull    # only if the cache is empty: download from the configured DVC remote",
                 f"verify: md5 of {meta['path']} must equal {meta['md5']}"]
        return {"rev": a.rev, "dvc_file": f, "expected_md5": meta["md5"], "git_commit": commit["commit"],
                "steps": steps, "note": "Read-only explanation; use checkout_dataset_version (needs confirmation) to execute."}

    def checkout_version(self, a: CheckoutArgs) -> dict[str, Any]:
        f = self._pick(a.dataset)
        meta = self._dvc_at(a.rev, f)
        self._git("checkout", _safe_rev(a.rev), "--", f)
        rc, out, err = self.runner.run(["dvc", "checkout", "--force", f])  # confirmed by user: overwrites local edits
        if rc != 0:
            raise ToolError(f"dvc checkout failed: {err or out}")
        return {"restored_rev": a.rev, "dvc_file": f, "expected_md5": meta["md5"], "dvc_output": out}

    def experiment_dataset(self, a: ExperimentRunRef) -> dict[str, Any]:
        if self._mlflow is None:
            raise ToolUnavailable("MLflow client not configured; cannot read recorded provenance")
        client = self._mlflow()
        run = None
        try:
            if a.run_id:
                run = client.get_run(a.run_id)
            else:
                exp = client.get_experiment_by_name(self.experiment)
                if exp is None:
                    raise ToolError(f"Experiment '{self.experiment}' not found")
                runs = client.search_runs([exp.experiment_id], max_results=1000, order_by=["start_time ASC"])
                if a.run_number:
                    runs = [r for r in runs if r.data.tags.get("run_number") == str(a.run_number)] or \
                        (runs[a.run_number - 1:a.run_number] if a.run_number <= len(runs) else [])
                if not runs:
                    raise ToolError("No matching MLflow run found")
                run = runs[-1] if not a.run_number else runs[0]
        except (ToolError, ToolUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolUnavailable(f"MLflow unreachable: {exc}") from exc
        tags = run.data.tags
        md5 = tags.get("dvc.md5")
        if not md5:
            return {"run_id": run.info.run_id, "provenance_recorded": False,
                    "note": "Run has no dvc.md5 tag - dataset version cannot be proven for this run."}
        out: dict[str, Any] = {"run_id": run.info.run_id, "run_number": tags.get("run_number"),
                               "provenance_recorded": True, "dvc_md5": md5, "dvc_file": tags.get("dvc.file"),
                               "recorded_git_commit": tags.get("git.commit"),
                               "recorded_dataset_version": tags.get("dvc.version")}
        # Independent verification against git history of the .dvc pointer
        try:
            f = tags.get("dvc.file") or self._pick(None)
            commits = self._git("log", "--format=%H", "--", f).splitlines()
            for c in commits:
                if self._dvc_at(c, f)["md5"] == md5:
                    out["verified_in_git"] = True
                    out["matching_commit"] = c
                    out["matching_tags"] = self._tags_at(c)
                    break
            else:
                out["verified_in_git"] = False
        except (ToolError, ToolDenied):
            out["verified_in_git"] = None
        return out

    def tools(self) -> list[Tool]:
        return [
            Tool("list_dvc_datasets", "List datasets tracked by DVC (hash, size, path) and DVC config.", NoArgs, self.list_datasets, "dvc"),
            Tool("get_dataset_version", "Get the current dataset version: md5 hash, git commit and version tags of the DVC pointer.", DatasetArgs, self.get_version, "dvc"),
            Tool("compare_dataset_versions", "Compare two dataset versions (git tags/commits) by DVC hash and size.", CompareDatasetArgs, self.compare_versions, "dvc"),
            Tool("detect_data_changes", "Detect whether tracked data in the workspace changed vs. its DVC-recorded hash (dvc status).", NoArgs, self.detect_changes, "dvc"),
            Tool("explain_dataset_reproduction", "Explain (read-only) the exact steps to reproduce a previous dataset version.", ReproduceArgs, self.explain_reproduce, "dvc"),
            Tool("get_experiment_dataset", "Read the DVC dataset version recorded in an MLflow run's provenance tags, verified against git history. Use run_id or run_number.", ExperimentRunRef, self.experiment_dataset, "dvc"),
            Tool("checkout_dataset_version", "Restore a dataset version into the workspace (git checkout of the .dvc pointer + dvc checkout --force; overwrites local modifications of the data file).", CheckoutArgs, self.checkout_version, "dvc", mutating=True),
        ]
