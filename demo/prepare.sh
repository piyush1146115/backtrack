#!/usr/bin/env bash
# Copy the demo package into a fresh folder with its own git repo, and enable Backtrack.
# Usage: demo/prepare.sh <target-dir>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
target="${1:?usage: demo/prepare.sh <target-dir>}"
if [ -e "$target" ]; then
  echo "$target already exists; pick a new folder" >&2
  exit 1
fi
mkdir -p "$target"
cp -R "$here/sessions" "$here/tests" "$here/pyproject.toml" "$here/.python-version" \
  "$here/.gitignore" "$here/uv.lock" "$target/"
cd "$target"
git init -q
git add .
git -c user.name=demo -c user.email=demo@localhost commit -qm "Session store demo"
uv run --project "$here/.." backtrack --dir "$target" install
