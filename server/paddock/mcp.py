"""Model Context Protocol server (Streamable HTTP transport, JSON responses).

Point any MCP client — OpenClaw, Claude Desktop/Code, Cursor, your own agent — at
    https://<paddock>:8443/mcp
with header  Authorization: Bearer <API token from Settings → AI agents>.
Clients that only speak stdio can use scripts/paddock_mcp_stdio.py as a bridge.

The token's scopes decide which tools the agent sees. Every write is audited.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import __version__, db, tools
from .actor import Actor

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "Paddock is a private, self-hosted vault for movies, TV, music, photos, home videos, RED cinema footage, "
    "CAD, 3D-print and electronics files, plus projects with video plans. Use search_media to find things "
    "(results include ids), get_item / get_project for detail, list_groups for albums, shows, shoot days and "
    "CAD part families. Write tools (tags, collections, projects, plans, rescans, trash) need a token with the "
    "'write' scope. Nothing can be permanently deleted through MCP."
)

router = APIRouter()


def _tool_entry(t: tools.Tool) -> dict:
    return {
        "name": t.name,
        "title": t.name.replace("_", " ").capitalize(),
        "description": t.description,
        "inputSchema": t.schema,
        "annotations": {
            "readOnlyHint": t.scope == "read",
            "destructiveHint": t.destructive,
            "idempotentHint": t.scope == "read",
            "openWorldHint": False,
        },
    }


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(actor: Actor, msg: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, params, id_ = msg["method"], msg.get("params") or {}, msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        db.audit(actor.audit_name, "mcp.initialize", client=params.get("clientInfo"), version=version)
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "paddock", "title": "Paddock", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method.startswith("notifications/"):
        return None
    elif method == "tools/list":
        result = {"tools": [_tool_entry(t) for t in tools.available(actor)]}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(id_, -32602, "arguments must be an object")
        t = tools.TOOLS.get(name)
        if t is None or not actor.can(t.scope):
            return _error(id_, -32602, f"Unknown tool: {name}")
        try:
            data = tools.call(actor, name, args)
            text = json.dumps(data, default=str, indent=None)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(data, dict):
                result["structuredContent"] = json.loads(text)
        except (tools.ToolError, ValueError, PermissionError) as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    else:
        return None if is_notification else _error(id_, -32601, f"Method not found: {method}")
    return None if is_notification else {"jsonrpc": "2.0", "id": id_, "result": result}


@router.post("/mcp")
async def mcp_post(request: Request):
    from .api.deps import actor_from_api_token

    actor = actor_from_api_token(request)
    if actor is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": 'Bearer realm="paddock-mcp"'})
    origin = request.headers.get("origin")
    if origin and not origin.startswith(("https://localhost", "http://localhost", "https://127.0.0.1")) \
            and origin.split("://", 1)[-1].split(":")[0] != request.url.hostname:
        return JSONResponse({"error": "origin not allowed"}, status_code=403)  # DNS-rebinding guard
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

    from starlette.concurrency import run_in_threadpool

    if isinstance(body, list):
        out = [r for r in [await run_in_threadpool(handle, actor, m) for m in body] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await run_in_threadpool(handle, actor, body)
    if res is None:
        return Response(status_code=202)
    return JSONResponse(res)


@router.get("/mcp")
async def mcp_get():
    # No server-initiated stream: this server only answers requests.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})
