"""Graph API stubs for graph-rag.

Bedrock Knowledge Base GraphRAG is used via MCP retrieve / RAG upload.
Local graphify HTML (agent-skills style) is not provisioned here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from application.api.routes_auth import require_user_id
from application import utils

router = APIRouter(prefix="/api/graph", tags=["graph"])


class GraphQueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)


@router.get("/status")
def graph_status(request: Request) -> dict:
    user_id = require_user_id(request)
    enabled = utils.is_knowledge_graph_enabled(user_id)
    return {
        "enabled": enabled,
        "available": False,
        "status": "unavailable",
        "message": (
            "Local Knowledge Graph UI is not available in graph-rag. "
            "Use Bedrock Knowledge Base (knowledge base MCP / RAG upload)."
        ),
    }


@router.post("/rebuild")
def rebuild_graph(request: Request) -> dict:
    require_user_id(request)
    raise HTTPException(
        status_code=501,
        detail="Local Knowledge Graph rebuild is not available in graph-rag",
    )


@router.get("")
def get_user_graph(request: Request):
    require_user_id(request)
    return HTMLResponse(
        "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Knowledge Graph UI unavailable</h1>"
        "<p>graph-rag uses Amazon Bedrock Knowledge Bases GraphRAG "
        "(Neptune). Use the <strong>knowledge base</strong> MCP server "
        "or document upload for retrieval.</p>"
        "</body></html>",
        status_code=200,
    )


@router.post("/query")
def query_graph(body: GraphQueryRequest, request: Request) -> dict:
    require_user_id(request)
    raise HTTPException(
        status_code=501,
        detail="Local graph query is not available; use knowledge base retrieve",
    )
