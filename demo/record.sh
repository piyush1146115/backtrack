#!/usr/bin/env bash
# Record the demo's "wrong path" session headlessly: 3 turns, the third removes the cache.
# Usage: demo/record.sh <prepared-dir>     (MODEL=sonnet demo/record.sh ... to change model)
set -euo pipefail
dir="${1:?usage: demo/record.sh <prepared-dir>}"
model="${MODEL:-haiku}"
cd "$dir"

run() {
  claude -p "$1" --model "$model" --permission-mode acceptEdits \
    --allowedTools "Bash(uv run pytest:*)" --output-format json "${@:2}" 2>/dev/null \
    | python3 -c 'import json, sys
for line in sys.stdin:
    try: o = json.loads(line)
    except ValueError: continue
    if "session_id" in o: print(o["session_id"])' | tail -1
}

sid=$(run "Run the test suite with: uv run pytest -q   and tell me what fails. Don't change anything yet.")
echo "session: $sid"
run "Read sessions/store.py and briefly explain how the cache works." --resume "$sid" >/dev/null
run "The cache is more trouble than it's worth. Fix the failing test by removing the caching from SessionStore entirely, then run the tests." --resume "$sid" >/dev/null
echo "Recorded 3 turns. Fork at turn 3 with a better instruction, for example:"
echo "  Fix the failing test but keep the cache: the expiry calculation looks wrong. Then run the tests."
