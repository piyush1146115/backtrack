# Demo: the session cache bug

`sessions/store.py` caches sessions in front of a slow backend. One test fails:
a revoked session is still served after its TTL, because the expiry time is computed
in milliseconds (`ttl * 1000`) while the clock is in seconds.

- The tempting fix, removing the cache, breaks three tests that rely on it.
- The right fix, correcting the expiry, passes all six.

## Run it

```sh
demo/prepare.sh /tmp/bt-demo      # fresh copy with its own git repo + Backtrack installed
demo/record.sh /tmp/bt-demo       # record 3 turns; turn 3 asks to remove the cache
uv run backtrack --dir /tmp/bt-demo serve
```

Then fork at turn 3 with a better instruction:

```sh
uv run backtrack --dir /tmp/bt-demo fork <session> 3 \
  "Fix the failing test but keep the cache: the expiry calculation looks wrong. Then run the tests." \
  --allow "Bash(uv run pytest:*)"
```

`record.sh` uses Haiku by default; set `MODEL=sonnet` to change it. You can also run
`claude` interactively in the prepared folder instead.

Compare the original with the fork (or open the fork in the timeline and follow
"compare with original"):

```sh
uv run backtrack --dir /tmp/bt-demo compare <session> <fork>
```

The original passes its own tests only because it rewrote them; with the original
tests it fails. The fork passes both.
