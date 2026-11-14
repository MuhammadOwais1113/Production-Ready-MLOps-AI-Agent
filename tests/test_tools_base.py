"""Registry-level reliability: validation, timeouts, error classes, confirmation gate, secret-free logs."""
from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel

from agent.logging_utils import redact
from agent.schemas import NoArgs
from agent.tools.base import Tool, ToolRegistry, ToolUnavailable


class Num(BaseModel):
    n: int


def make(func, args=Num, mutating=False, timeout=0.3):
    reg = ToolRegistry(timeout_s=timeout)
    reg.register([Tool("t", "d", args, func, "x", mutating=mutating)])
    return reg


def test_invalid_arguments_are_rejected_without_calling_tool():
    called = []
    reg = make(lambda a: called.append(1))
    r = reg.execute("t", {"n": "not-a-number"})
    assert r.status == "invalid" and "n" in r.error and not called


def test_unknown_tool():
    assert make(lambda a: 1).execute("nope", {}).status == "unknown_tool"


def test_tool_timeout_is_reported():
    reg = make(lambda a: time.sleep(1.5))
    r = reg.execute("t", {"n": 1})
    assert r.status == "timeout" and "0.3" in r.error


def test_service_failure_is_classified_not_raised():
    def boom(a):
        raise ToolUnavailable("mlflow down")
    r = make(boom).execute("t", {"n": 1})
    assert r.status == "unavailable" and r.data is None

    def crash(a):
        raise RuntimeError("bug")
    assert make(crash).execute("t", {"n": 1}).status == "error"


def test_mutating_tool_requires_confirmation():
    ran = []
    reg = make(lambda a: ran.append(1) or "done", args=NoArgs, mutating=True)
    assert reg.execute("t", {}).status == "needs_confirmation" and not ran
    assert reg.execute("t", {}, confirmed=True).status == "ok" and ran


def test_structured_logs_have_name_status_duration_requestid_and_no_secrets(caplog):
    reg = make(lambda a: "ok")
    with caplog.at_level(logging.INFO, logger="mlops_agent.tools"):
        reg.execute("t", {"n": 1, "api_key": "sk-supersecretvalue123", "password": "hunter2"}, request_id="req-42")
    rec = [r for r in caplog.records if r.getMessage() == "tool_call"][0]
    from agent.logging_utils import JsonFormatter
    line = JsonFormatter().format(rec)
    doc = json.loads(line)
    assert doc["tool"] == "t" and doc["status"] == "ok" and doc["request_id"] == "req-42" and "duration_ms" in doc
    assert "hunter2" not in line and "sk-supersecret" not in line


def test_redact_masks_bearer_tokens_in_values():
    out = redact({"msg": "Authorization failed for Bearer abc.def.ghi", "nested": {"token": "x"}})
    assert "abc.def.ghi" not in json.dumps(out) and out["nested"]["token"] == "***REDACTED***"
