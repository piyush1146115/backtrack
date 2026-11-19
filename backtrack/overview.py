"""Data for the home page: every session's turns, and the latest test result at the end of
each branch, for drawing sessions and their forks as a graph."""

from __future__ import annotations

from dataclasses import asdict

from . import audit, compare, rank
from .snapshot.shadow import ShadowRepo
from .store.base import Store

MAX_PROMPT = 160


def _one_line(text: str, n: int = MAX_PROMPT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def overview(store: Store, shadow: ShadowRepo, command: str, globs: list[str]) -> dict:
    sessions = store.list_sessions()
    out = []
    for s in sessions:
        events = store.get_events(s.session_id)
        turns = []
        for e in events:
            if e.kind == "prompt":
                prompt = (e.input or {}).get("prompt", "")
                if s.parent_session and prompt.startswith("[Backtrack] ") and "\n\n" in prompt:
                    prompt = prompt.split("\n\n", 1)[1]
                turns.append({"turn": e.turn, "seq": e.seq, "ts": e.ts, "prompt": _one_line(prompt),
                              "snapshot": bool(e.snapshot_ref), "irreversible": 0})
            elif e.kind == "tool_pre" and e.irreversible and turns:
                turns[-1]["irreversible"] += 1
        out.append(asdict(s) | {"turn_list": turns, "status": rank.status(events), "tests": None,
                                "audit": audit.audit_or_none(store, shadow, s, globs, command)})

    # Cached test results only: the home page never runs tests.
    by_id = {s["session_id"]: s for s in out}
    groups = sorted({(s["parent_session"], s["fork_turn"]) for s in out if s["parent_session"] in by_id},
                    key=lambda g: max(x["started_at"] for x in out
                                      if (x["parent_session"], x["fork_turn"]) == g))
    for parent, turn in groups:  # later groups win for the parent's own result
        try:
            base, found = rank.branches(store, shadow, parent, turn)
        except rank.RankError:
            continue
        rank.add_tests(store, shadow, base, found, command, globs)
        for b in found:
            if b.original and b.session.parent_session:
                continue  # a fork's result is against its own fork point
            d = rank.to_dict(b)
            if d["tests_original"] or d["own"]:
                by_id[b.session.session_id]["tests"] = {
                    "own": d["own"], "original": d["tests_original"], "gamed": d["gamed"],
                    "tests_changed": d["tests_changed"], "turn": turn, "parent": parent}
    for s in out:
        if s["tests"] is None and not s["parent_session"]:
            ref = compare.end_ref(store, next(x for x in sessions if x.session_id == s["session_id"]))
            run = ref and store.find_test_run(ref, ref, command)
            if run:
                s["tests"] = {"own": asdict(run), "original": None, "gamed": False, "tests_changed": False}
    return {"command": command, "sessions": out}
