"""Flag tool calls with effects outside the workspace ("points of no return")."""

from __future__ import annotations

import re

_PATTERNS = [
    r"\bgit\s+push\b",
    r"\bcurl\b.*(-X\s*|--request\s+)(POST|PUT|PATCH|DELETE)\b",
    r"\bcurl\b.*\s(-d|--data\S*|-F|--form|-T|--upload-file)\b",
    r"\bwget\b.*--(post-data|post-file|method=(POST|PUT|PATCH|DELETE))",
    r"\b(npm|yarn|pnpm)\s+publish\b",
    r"\btwine\s+upload\b",
    r"\bcargo\s+publish\b",
    r"\bgem\s+push\b",
    r"\bdocker\s+push\b",
    r"\bgh\s+(pr\s+(create|merge|close|comment)|issue\s+(create|close|comment)|release\s+create)\b",
    r"\bgh\s+api\b.*(-X\s*|--method\s+)(POST|PUT|PATCH|DELETE)\b",
    r"\bkubectl\s+(apply|create|delete|patch|replace|scale)\b",
    r"\bterraform\s+(apply|destroy)\b",
    r"\bhelm\s+(install|upgrade|uninstall|delete|rollback)\b",
]
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)


def is_irreversible(tool_name: str | None, tool_input) -> bool:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    return bool(_RE.search(tool_input.get("command", "")))
