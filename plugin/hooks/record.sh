#!/bin/sh
# Hands each Claude Code hook event to the backtrack CLI, which records it.
# Fails open: without the CLI it does nothing, apart from one hint at session start.
for hook in "$(command -v backtrack-hook)" "$HOME/.local/bin/backtrack-hook"; do
  if [ -n "$hook" ] && [ -x "$hook" ]; then
    exec "$hook"
  fi
done
if [ "$1" = "start" ]; then
  echo '{"systemMessage": "Backtrack plugin: the backtrack CLI is not installed, so this session is not recorded. Install it with: uv tool install git+https://github.com/piyush1146115/backtrack"}'
fi
exit 0
