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

# Forward args to the trader CLI, injecting --config unless the caller already passed one.
case " $* " in
    *" --config "*) exec trader "$@" ;;
    *) exec trader "$@" --config "$CONFIG" ;;
esac
