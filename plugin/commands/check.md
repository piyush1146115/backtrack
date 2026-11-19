---
description: Check that Backtrack is recording this project
allowed-tools: Bash(backtrack doctor:*), Bash(true)
---

!`backtrack doctor || true`

Summarise this report in one or two lines: is this project being recorded, and if not, what is the fix it gives. If `backtrack` was not found, the CLI is missing: it installs with `uv tool install git+https://github.com/piyush1146115/backtrack`.
