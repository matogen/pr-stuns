#!/usr/bin/env bash
# Watchdog script for run_continuous.py
#
# Usage: watchdog.sh {start|stop|restart|status}
#   start   (default) start the reviewer if it isn't already running
#   stop    stop the reviewer
#   restart stop then start — use this to load new code after a deploy
#   status  print whether the reviewer is running
#
# Intended to be called by cron every hour with no arguments (= start).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
RUN_SCRIPT="$SCRIPT_DIR/run_continuous.py"
LOG_FILE="$SCRIPT_DIR/watchdog.log"
PID_FILE="$SCRIPT_DIR/run_continuous.pid"
MAX_LOG_BYTES=$((10 * 1024 * 1024))  # 10MB; watchdog.log only gets a few lines/hour, so this is a generous ceiling

rotate_log_if_needed() {
    if [[ -f "$LOG_FILE" ]]; then
        local size
        size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
        if (( size > MAX_LOG_BYTES )); then
            mv -f "$LOG_FILE" "$LOG_FILE.1"
        fi
    fi
}

log() {
    rotate_log_if_needed
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(<"$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            # Double-check it's actually our script
            if ps -p "$pid" -o args= 2>/dev/null | grep -q "run_continuous.py"; then
                return 0
            fi
        fi
        # PID file is stale
        rm -f "$PID_FILE"
    fi
    return 1
}

start_process() {
    log "Starting run_continuous.py..."
    # cd as its own statement, NOT chained with `&&` into the backgrounded
    # command. `cd X && CMD &` backgrounds the whole `&&` list as one job,
    # which forces bash to fork a subshell that waits on CMD to report the
    # job's exit status — that subshell then blocks in wait4() for as long
    # as CMD runs (i.e. indefinitely for this long-lived reviewer process),
    # so this script would never actually exit. A plain `CMD &` after a
    # separate `cd` lets bash background CMD directly with no such wrapper.
    cd "$SCRIPT_DIR" || { log "Failed to cd to $SCRIPT_DIR"; return 1; }
    # Python logging writes to pr_reviewer.log (rotated, size-capped); discard
    # the duplicate console stream. watchdog.log itself is rotated by
    # rotate_log_if_needed() in log() above.
    # 200>&- closes our flock fd in the child so the long-running reviewer
    # process doesn't inherit and hold it open — otherwise every future
    # watchdog.sh invocation would find the lock permanently held and exit
    # immediately, since flock() releases only when every fd referencing the
    # open file description is closed, not when this script's shell exits.
    nohup "$VENV_PYTHON" "$RUN_SCRIPT" >/dev/null 2>&1 200>&- &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log "Started with PID $pid"
}

stop_process() {
    if is_running; then
        local pid
        pid=$(<"$PID_FILE")
        log "Stopping run_continuous.py (PID $pid)..."
        kill "$pid" 2>/dev/null
        # Wait for a clean exit, then force-kill if needed
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "PID $pid did not exit, sending SIGKILL"
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PID_FILE"
        log "Stopped."
    else
        log "run_continuous.py is not running; nothing to stop."
    fi
}

ACTION="${1:-start}"

# `status` is read-only and should always answer promptly, even if another
# start/stop/restart is in flight — skip locking for it.
if [[ "$ACTION" == "status" ]]; then
    if is_running; then
        echo "running (PID $(<"$PID_FILE"))"
    else
        echo "not running"
    fi
    exit 0
fi

# start/stop/restart all check-then-act on $PID_FILE. Serialize them with a
# non-blocking flock so an overlapping cron/@reboot invocation (e.g. a reboot
# landing exactly on the hourly tick) can't race past the is_running check
# before either one has written the PID file, which would double-spawn the
# reviewer process.
LOCK_FILE="$SCRIPT_DIR/watchdog.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Another watchdog invocation is already handling start/stop/restart; exiting."
    exit 0
fi

case "$ACTION" in
    start)
        if is_running; then
            log "run_continuous.py is already running (PID $(<"$PID_FILE")). Nothing to do."
        else
            log "run_continuous.py is not running. Starting it..."
            start_process
        fi
        ;;
    stop)
        stop_process
        ;;
    restart)
        log "Restart requested."
        stop_process
        start_process
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}" >&2
        exit 1
        ;;
esac
