import json
import threading
import time

import pytest

from backtrack.hooks.irreversible import is_irreversible
from backtrack.transcript import last_turn_summary, wait_for_turn_end


def usage(i, o):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 10}


RECORDS = [
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "user", "uuid": "p1", "message": {"role": "user", "content": "turn one"}},
    {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "usage": usage(1, 5),
     "content": [{"type": "text", "text": "ok"}]}},
    {"type": "user", "uuid": "p2", "message": {"role": "user",
     "content": [{"type": "text", "text": "turn two"}]}},
    {"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": "injected"}},
    # One API response split into two records with the same message id and usage.
    {"type": "assistant", "uuid": "a2", "message": {"id": "m2", "usage": usage(2, 7),
     "content": [{"type": "thinking", "thinking": ""}]}},
    {"type": "assistant", "uuid": "a3", "message": {"id": "m2", "usage": usage(2, 7),
     "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
    {"type": "user", "uuid": "r1", "toolUseResult": {"stdout": "hi"},
     "message": {"content": [{"type": "tool_result", "content": "hi"}]}},
    {"type": "assistant", "uuid": "side", "isSidechain": True,
     "message": {"id": "mx", "usage": usage(999, 999), "content": []}},
    {"type": "assistant", "uuid": "a4", "message": {"id": "m3", "usage": usage(3, 11),
     "content": [{"type": "text", "text": "done"}]}},
    {"type": "user", "uuid": "meta2", "isMeta": True, "message": {"content": "injected"}},
    {"type": "cost-state", "totalCostUSD": 0.01},
]


def test_last_turn_summary(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n{partial")
    s = last_turn_summary(path)
    assert s.leaf_uuid == "a4"
    assert s.tokens == {"input": 5, "output": 18, "cache_read": 200, "cache_creation": 20}


def test_last_turn_summary_records_the_model(tmp_path):
    path = tmp_path / "t.jsonl"
    records = [dict(r, message=dict(r["message"], model="claude-haiku-5-5"))
               if r.get("uuid") == "a4" else r for r in RECORDS]
    path.write_text("\n".join(json.dumps(r) for r in records))
    assert last_turn_summary(path).tokens["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push --force",
    "curl -X POST https://api.example.com/x",
    "curl --request DELETE https://api.example.com/x/1",
    "curl -d '{\"a\":1}' https://api.example.com",
    "npm publish",
    "gh pr create --fill",
    "kubectl apply -f deploy.yaml",
    "terraform apply -auto-approve",
])
def test_irreversible(command):
    assert is_irreversible("Bash", {"command": command})


@pytest.mark.parametrize("command", [
    "git status", "git commit -m push", "curl https://example.com", "pytest -q",
    "ls push", "gh pr view 12",
])
def test_reversible(command):
    assert not is_irreversible("Bash", {"command": command})


def test_non_bash_never_irreversible():
    assert not is_irreversible("Write", {"command": "git push"})


def test_wait_for_turn_end_picks_up_late_final_message(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS[:8]) + "\n")  # ends at tool result
    final = RECORDS[9]

    def append_later():
        time.sleep(0.2)
        with open(path, "a") as f:
            f.write(json.dumps(final) + "\n")

    threading.Thread(target=append_later).start()
    s = wait_for_turn_end(path, "done", timeout=2)
    assert s.leaf_uuid == "a4" and s.tokens["output"] == 18


def test_wait_for_turn_end_times_out_with_what_is_there(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS[:8]) + "\n")
    s = wait_for_turn_end(path, "never written", timeout=0.1)
    assert s.leaf_uuid == "r1"


def test_wait_for_turn_end_ignores_same_text_in_earlier_turn(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS[:8]) + "\n")
    start = time.monotonic()
    s = wait_for_turn_end(path, "ok", timeout=0.3)  # "ok" is turn one's reply
    assert time.monotonic() - start >= 0.3 and s.leaf_uuid == "r1"
