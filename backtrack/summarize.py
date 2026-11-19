"""Truncating tool inputs and outputs before they go into the event log."""

from __future__ import annotations

import json

MAX_OUTPUT = 2000
MAX_INPUT_STRING = 4000


def truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more chars]"


def shrink(value):
    """Truncate long strings inside a tool input (e.g. file contents passed to Write)."""
    if isinstance(value, str):
        return truncate(value, MAX_INPUT_STRING)
    if isinstance(value, dict):
        return {k: shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shrink(v) for v in value]
    return value


def summarize_response(response) -> str | None:
    if response is None or isinstance(response, str):
        return truncate(response, MAX_OUTPUT)
    if isinstance(response, dict):
        if "stdout" in response or "stderr" in response:
            parts = [response.get("stdout") or "", response.get("stderr") or ""]
            return truncate("\n".join(p for p in parts if p), MAX_OUTPUT)
        if "filePath" in response:  # Write/Edit responses carry whole file contents
            return f"{response.get('type', 'update')} {response['filePath']}"
        if isinstance(response.get("file"), dict) and "filePath" in response["file"]:  # Read
            f = response["file"]
            return f"read {f['filePath']} ({f.get('numLines', '?')} lines)"
    return truncate(json.dumps(response), MAX_OUTPUT)
