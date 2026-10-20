#!/usr/bin/env python3
"""stdio ↔ Paddock MCP bridge, for MCP clients that only launch local commands.

No dependencies beyond the Python standard library. Configure with environment variables:

  PADDOCK_URL    https://192.168.1.50:8443        (or the tunnel address)
  PADDOCK_TOKEN  pdk_ai_...                        (Manage server → AI agents)
  PADDOCK_CA     /path/to/paddock-ca.crt           (Manage server → Remote access → download certificate)

Example client config:
  {"mcpServers": {"paddock": {"command": "python3", "args": ["paddock_mcp_stdio.py"],
    "env": {"PADDOCK_URL": "https://192.168.1.50:8443", "PADDOCK_TOKEN": "pdk_ai_...", "PADDOCK_CA": "paddock-ca.crt"}}}}
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

URL = os.environ.get("PADDOCK_URL", "").rstrip("/") + "/mcp"
TOKEN = os.environ.get("PADDOCK_TOKEN", "")
CA = os.environ.get("PADDOCK_CA")


def context() -> ssl.SSLContext:
    if not CA:
        sys.exit("Set PADDOCK_CA to the Paddock certificate (refusing to connect without verifying the server).")
    ctx = ssl.create_default_context(cafile=CA)
    ctx.check_hostname = False  # Paddock's private CA is the pin; LAN/tunnel addresses vary
    return ctx


def forward(line: str, ctx: ssl.SSLContext) -> str | None:
    req = urllib.request.Request(URL, data=line.encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=300) as r:
            body = r.read().decode()
            return body if r.status != 202 and body.strip() else None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = {}
        if "id" not in msg:
            return None
        return json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": f"Paddock HTTP {e.code}"}})
    except urllib.error.URLError as e:
        msg = json.loads(line) if line.strip().startswith("{") else {}
        if "id" not in msg:
            return None
        return json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": f"Paddock unreachable: {e.reason}"}})


def main() -> None:
    if not URL.startswith("https://") or not TOKEN:
        sys.exit("Set PADDOCK_URL (https://...) and PADDOCK_TOKEN.")
    ctx = context()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out = forward(line, ctx)
        if out:
            sys.stdout.write(out.strip() + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
