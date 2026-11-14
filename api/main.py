"""FastAPI interface: request validation and response shaping in front of the agent."""
from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agent.main import build_agent
from agent.orchestrator import Orchestrator
from agent.schemas import ChatRequest, ChatResponse

app = FastAPI(title="MLOps AI Agent", version="1.0.0")


@lru_cache
def get_agent() -> Orchestrator:
    return build_agent()


@app.middleware("http")
async def request_id_header(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.rid = rid
    resp = await call_next(request)
    resp.headers["x-request-id"] = rid
    return resp


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "internal error", "type": type(exc).__name__})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools")
def tools(agent: Orchestrator = Depends(get_agent)) -> list[dict]:
    return [{"name": t["name"], "description": t["description"]} for t in agent.reg.schemas()]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, agent: Orchestrator = Depends(get_agent)) -> ChatResponse:
    if not req.request_id:
        req.request_id = request.state.rid
    return agent.handle(req)
