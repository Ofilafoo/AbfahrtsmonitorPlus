#!/bin/sh
set -eu

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
pid_file="$runtime_dir/rsag-pir.pid"
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
log_file="$project_dir/pir.log"
config_file=${RSAG_DASHBOARD_ENV:-$HOME/.config/rsag-dashboard.env}
if [ -f "$config_file" ]; then
    . "$config_file"
fi

exec 9>"$runtime_dir/rsag-dashboard.lock"
if ! flock -n 9; then
    exit 0
fi

sensor_pid=
gui_pid=
cleanup() {
    trap - EXIT HUP INT TERM
    if [ -n "$gui_pid" ]; then
        kill "$gui_pid" 2>/dev/null || true
        wait "$gui_pid" 2>/dev/null || true
    fi
    if [ -n "$sensor_pid" ]; then
        kill "$sensor_pid" 2>/dev/null || true
        wait "$sensor_pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
    vcgencmd display_power 1 >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 0' HUP INT TERM

/usr/bin/python3 -u "$HOME/Desktop/pir2.py" >"$log_file" 2>&1 &
sensor_pid=$!
printf '%s\n' "$sensor_pid" >"$pid_file"

/usr/bin/python3 "$project_dir/dashboard_gui.py" &
gui_pid=$!
wait "$gui_pid"
