#!/bin/sh
# Exec-form entrypoint so the trader process becomes PID 1 and receives SIGTERM directly —
# giving the daemon a clean shutdown (finish/abort the running cycle, release the global
# lock, flush state) instead of a SIGKILL after the grace period.
set -eu

CONFIG="${TRADER_CONFIG:-/app/config/default.yaml}"

# Default command is the paper/live daemon.
if [ "$#" -eq 0 ]; then
    set -- run
fi

# Forward args to the trader CLI, injecting --config unless the caller already passed one
# (check each arg so a value containing "--config" can't false-match; cover -c/--config=).
for arg in "$@"; do
    case "$arg" in
        -c | --config | --config=*)
            exec trader "$@"
            ;;
    esac
done
exec trader "$@" --config "$CONFIG"
