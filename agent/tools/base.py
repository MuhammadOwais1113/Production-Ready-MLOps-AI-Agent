"""Tool abstraction + registry. Every tool call goes through ToolRegistry.execute which provides:
argument validation, confirmation gating for mutating tools, timeouts, error classification,
and structured (secret-free) logging of tool name / status / duration / request id."""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from agent.logging_utils import get_logger, log_event
from agent.schemas import ToolResult

logger = get_logger("mlops_agent.tools")


class ToolUnavailable(Exception):
    """The backing service (MLflow, Airflow, K8s, DVC) cannot be reached."""


class ToolDenied(Exception):
    """The operation violates a safety policy (namespace, allow-list, command allow-list)."""


class ToolError(Exception):
    """The backing service answered but the operation failed."""


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., Any]
    domain: str  # dvc | mlflow | airflow | kubernetes
    mutating: bool = False

    def anthropic_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description + (
            " [STATE-CHANGING: requires explicit user confirmation]" if self.mutating else ""), "input_schema": schema}


class ToolRegistry:
    def __init__(self, timeout_s: float = 20.0) -> None:
        self._tools: dict[str, Tool] = {}
        self.timeout_s = timeout_s
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

    def register(self, tools: list[Tool]) -> None:
        for t in tools:
            self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def by_domain(self, domain: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.domain == domain]

    def schemas(self) -> list[dict[str, Any]]:
        return [t.anthropic_schema() for t in self._tools.values()]

    def execute(self, name: str, raw_args: dict[str, Any] | None, *, request_id: str | None = None,
                confirmed: bool = False) -> ToolResult:
        rid = request_id or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        status, data, error = "ok", None, None
        tool = self._tools.get(name)
        try:
            if tool is None:
                status, error = "unknown_tool", f"No such tool: {name}"
            else:
                try:
                    args = tool.args_model(**(raw_args or {}))
                except ValidationError as exc:
                    status = "invalid"
                    error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                else:
                    if tool.mutating and not confirmed:
                        status = "needs_confirmation"
                        error = (f"'{name}' changes system state. Ask the user to explicitly confirm "
                                 "(re-send the request with confirm=true).")
                    else:
                        fut = self._pool.submit(tool.func, args)
                        try:
                            data = fut.result(timeout=self.timeout_s)
                        except FutTimeout:
                            fut.cancel()
                            status, error = "timeout", f"Tool '{name}' exceeded {self.timeout_s}s"
        except ToolUnavailable as exc:
            status, error = "unavailable", str(exc)
        except ToolDenied as exc:
            status, error = "denied", str(exc)
        except ToolError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - never let a tool crash the agent
            status, error = "error", f"{type(exc).__name__}: {exc}"
        duration = round((time.perf_counter() - start) * 1000, 2)
        log_event(logger, "tool_call", tool=name, status=status, duration_ms=duration, request_id=rid,
                  args=raw_args or {}, error=error)
        return ToolResult(tool=name, status=status, data=data, error=error, duration_ms=duration, request_id=rid)  # type: ignore[arg-type]
