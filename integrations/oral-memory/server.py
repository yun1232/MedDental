"""Dependency-free stdio MCP endpoint mounted by DSH's existing MCP client."""

import json
import os
import sqlite3
import sys
from pathlib import Path

from schemas import MemoryError, tools, validate
from store import MemoryStore, settings_from_env

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_LINE_BYTES = 1_000_000


def instructions():
    """Read the same operational instructions used by the DSH system-prompt overlay."""
    return (Path(__file__).parent / "prompts" / "memory-policy.zh.txt").read_text(encoding="utf-8")


def call(store, name, arguments):
    """Dispatch only role-visible tools and validate all model-provided arguments."""
    declarations = {item["name"]: item for item in tools(store.role)}
    if name not in declarations:
        raise MemoryError("unknown_tool", "Tool is unavailable for this host role")
    validate(arguments, declarations[name]["inputSchema"])
    if name.startswith("put_"):
        return store.put(name.removeprefix("put_"), arguments)
    if name == "get_patient_experience":
        return store._deliver(store.get_patient_experience(arguments))
    if name == "retrieve":
        return store.retrieve(arguments)
    if name == "search":
        return store.search(arguments)
    if name == "get":
        return store._deliver(store.get(arguments))
    if name in ("memory_feedback", "set_frequency"):
        return getattr(store, name)(arguments)
    return {"policy": instructions(), "patient_id": store.patient_id, "actor_role": store.role,
            "encounter_id": os.environ.get("ORAL_MEMORY_ENCOUNTER_ID", "current")}


def dispatch(store, message):
    """Implement MCP discovery, lifecycle and tools over newline-delimited JSON-RPC."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message:
        return None
    request_id = message["id"]
    method = message["method"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "dsh-oral-memory", "version": "2.0.0"},
                  "instructions": instructions()}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools(store.role)}
    elif method == "tools/call":
        try:
            if not isinstance(params.get("name"), str):
                raise MemoryError("invalid_input", "A tool name is required")
            value = call(store, params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                      "structuredContent": value, "isError": False}
        except MemoryError as error:
            result = {"content": [{"type": "text", "text": json.dumps(
                {"code": error.code, "message": str(error)}, ensure_ascii=False)}], "isError": True}
        except (sqlite3.Error, OSError, ValueError, KeyError) as error:
            print(f"oral-memory operation failed: {type(error).__name__}", file=sys.stderr)
            result = {"content": [{"type": "text", "text": "Memory operation failed; no successful write is confirmed. Check server diagnostics."}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    """Serve the host-bound patient until stdin closes, then close SQLite."""
    try:
        settings = settings_from_env(os.environ)
        database = os.environ.get("ORAL_MEMORY_DB", "").strip()
        if not database:
            raise MemoryError("invalid_config", "ORAL_MEMORY_DB is required")
        store = MemoryStore(database, os.environ.get("ORAL_MEMORY_PATIENT_ID", ""),
                            os.environ.get("ORAL_MEMORY_ACTOR_ROLE", "patient"), settings)
    except (MemoryError, OSError, sqlite3.Error) as error:
        print(f"oral-memory startup failed: {error}", file=sys.stderr)
        return 1
    try:
        while True:
            raw = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES:
                print("oral-memory: oversized JSON-RPC line; closing transport", file=sys.stderr)
                return 1
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                response = dispatch(store, message)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
