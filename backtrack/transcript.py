"""Reading Claude Code session transcripts (~/.claude/projects/<dir>/<session>.jsonl)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TurnSummary:
    tokens: dict = field(default_factory=dict)
    leaf_uuid: str | None = None  # cut point for `claude --resume-session-at`


def read_records(path: str | Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a partially written last line
    return records


def prompt_text(record: dict) -> str:
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    return ""


def is_prompt(record: dict) -> bool:
    """A user-typed prompt, as opposed to a tool result or injected meta message."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("toolUseResult"):
        return False
    content = record.get("message", {}).get("content")
    if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
        return False
    text = prompt_text(record)
    # Local slash commands (/model, …) and their output are logged as user records.
    return bool(text) and not text.lstrip().startswith(("<local-command", "<command-name>"))


def _last_turn(path: str | Path) -> list[dict]:
    """Main-chain user/assistant records from the most recent prompt onwards."""
    chain = [r for r in read_records(path)
             if r.get("type") in ("user", "assistant") and not r.get("isSidechain")]
    start = max((i for i, r in enumerate(chain) if is_prompt(r)), default=0)
    return chain[start:]


def last_turn_summary(path: str | Path) -> TurnSummary:
    """Token usage and last message uuid of the most recent turn in a transcript."""
    turn = _last_turn(path)
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    seen = set()
    for r in turn:
        msg = r.get("message", {})
        # One API response is split into several records sharing message.id and usage.
        if r["type"] != "assistant" or msg.get("id") in seen:
            continue
        seen.add(msg.get("id"))
        usage = msg.get("usage") or {}
        tokens["input"] += usage.get("input_tokens", 0)
        tokens["output"] += usage.get("output_tokens", 0)
        tokens["cache_read"] += usage.get("cache_read_input_tokens", 0)
        tokens["cache_creation"] += usage.get("cache_creation_input_tokens", 0)
        if msg.get("model") and not msg["model"].startswith("<"):  # skip "<synthetic>"
            tokens["model"] = msg["model"]
    # Injected meta records after the reply belong to the next turn, not the cut point.
    leaves = [r for r in turn if not r.get("isMeta")]
    return TurnSummary(tokens=tokens, leaf_uuid=leaves[-1]["uuid"] if leaves else None)


def _has_message(path: str | Path, text: str) -> bool:
    for r in _last_turn(path):
        if r.get("type") != "assistant":
            continue
        for block in r.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip() == text:
                return True
    return False


def wait_for_turn_end(path: str | Path, last_message: str | None,
                      timeout: float = 3.0, interval: float = 0.05) -> TurnSummary:
    """last_turn_summary, once the turn's final assistant message is in the transcript.

    Claude Code can run the Stop hook before it has written the final message, which
    would leave the cut point one record short and miss that message's tokens.
    """
    text = (last_message or "").strip()
    deadline = time.monotonic() + timeout
    while text and not _has_message(path, text) and time.monotonic() < deadline:
        time.sleep(interval)
    return last_turn_summary(path)
