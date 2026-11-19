"""Import past Claude Code sessions from their transcripts.

Imported sessions have the same event kinds as recorded ones but no workspace
snapshots, since the files at the time were never captured.
"""

from __future__ import annotations

import re
from pathlib import Path

from .hooks.irreversible import is_irreversible
from .store.base import Event, Session, Store
from .summarize import MAX_OUTPUT, shrink, summarize_response, truncate
from .transcript import is_prompt, prompt_text, read_records


def transcript_dir(root: Path) -> Path:
    """Where Claude Code keeps transcripts for sessions started in `root`."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def _ts(record: dict) -> str:
    return record["timestamp"].replace("Z", "+00:00")


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    return content if isinstance(content, str) else ""


class _TurnState:
    def __init__(self):
        self.open = False
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.message_ids: set = set()
        self.last_uuid = None
        self.last_text = None
        self.last_ts = None

    def add_usage(self, msg: dict) -> None:
        if msg.get("id") in self.message_ids:
            return  # one API response is split across records sharing usage
        self.message_ids.add(msg.get("id"))
        usage = msg.get("usage") or {}
        self.tokens["input"] += usage.get("input_tokens", 0)
        self.tokens["output"] += usage.get("output_tokens", 0)
        self.tokens["cache_read"] += usage.get("cache_read_input_tokens", 0)
        self.tokens["cache_creation"] += usage.get("cache_creation_input_tokens", 0)


def import_transcript(store: Store, path: str | Path) -> str | None:
    """Append a transcript's session to the store. Returns its id, or None if skipped
    (empty, or already in the store: the log is append-only, so no re-import)."""
    chain = [r for r in read_records(path)
             if r.get("type") in ("user", "assistant") and not r.get("isSidechain")
             and r.get("timestamp")]
    if not chain:
        return None
    sid = chain[0].get("sessionId") or Path(path).stem
    if any(s.session_id == sid for s in store.list_sessions()):
        return None

    store.ensure_session(Session(session_id=sid, started_at=_ts(chain[0]),
                                 cwd=chain[0].get("cwd"), transcript_path=str(path)))
    store.append_event(Event(sid, "session_start", ts=_ts(chain[0]), input={"source": "import"}))
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, dict] = {}
    turn = _TurnState()

    def close_turn():
        if turn.open:
            store.append_event(Event(sid, "stop", ts=turn.last_ts,
                                     output=truncate(turn.last_text, MAX_OUTPUT),
                                     tokens=turn.tokens, leaf_uuid=turn.last_uuid))

    for r in chain:
        if is_prompt(r):
            close_turn()
            turn = _TurnState()
            turn.open = True
            store.append_event(Event(sid, "prompt", ts=_ts(r),
                                     input={"prompt": prompt_text(r), "prompt_id": r.get("promptId")}))
        elif r["type"] == "assistant":
            msg = r.get("message", {})
            turn.add_usage(msg)
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    turn.last_text = block.get("text")
                elif block.get("type") == "tool_use":
                    tool_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
                    tool_names[tool_id], tool_inputs[tool_id] = name, tool_input
                    store.append_event(Event(sid, "tool_pre", ts=_ts(r), tool_name=name,
                                             tool_use_id=tool_id, input=shrink(tool_input),
                                             irreversible=is_irreversible(name, tool_input)))
        else:
            content = r.get("message", {}).get("content")
            results = [b for b in content if b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for block in results:
                tool_id = block.get("tool_use_id")
                name = tool_names.get(tool_id)
                structured = r.get("toolUseResult") if len(results) == 1 else None
                output = summarize_response(structured) if isinstance(structured, dict) \
                    else truncate(_result_text(block), MAX_OUTPUT)
                store.append_event(Event(sid, "tool_post", ts=_ts(r), tool_name=name,
                                         tool_use_id=tool_id, output=output,
                                         status="failed" if block.get("is_error") else "ok",
                                         irreversible=is_irreversible(name, tool_inputs.get(tool_id))))
        if not r.get("isMeta"):  # injected context belongs to the next turn
            turn.last_uuid, turn.last_ts = r["uuid"], _ts(r)
    close_turn()
    return sid
