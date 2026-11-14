"""CLI entry point: `python -m agent.main` starts an interactive chat; `--ask "..."` answers one question."""
from __future__ import annotations

import argparse
import json
import sys

from agent.config import Settings
from agent.orchestrator import Orchestrator, build_registry
from agent.schemas import ChatRequest


def build_agent(settings: Settings | None = None) -> Orchestrator:
    s = settings or Settings()
    return Orchestrator(build_registry(s), s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MLOps AI agent")
    p.add_argument("--ask", help="ask one question and exit")
    p.add_argument("--confirm", action="store_true", help="explicitly confirm state-changing operations")
    p.add_argument("--json", action="store_true", help="print the full structured response")
    a = p.parse_args(argv)
    agent = build_agent()

    def ask(text: str) -> None:
        r = agent.handle(ChatRequest(message=text, confirm=a.confirm))
        print(json.dumps(r.model_dump(), indent=2, default=str) if a.json else f"[{r.mode} | request {r.request_id}]\n{r.answer}\n")

    if a.ask:
        ask(a.ask)
        return 0
    print("MLOps agent. Type a question (Ctrl+C / 'exit' to quit). Prefix with '!confirm ' to confirm state-changing actions.")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line in ("exit", "quit"):
            return 0
        if line:
            if line.startswith("!confirm "):
                a.confirm, line = True, line[9:]
            else:
                a.confirm = False
            ask(line)


if __name__ == "__main__":
    sys.exit(main())
