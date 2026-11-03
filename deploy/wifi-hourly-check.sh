#!/bin/sh
set -eu
: "${RSAG_WIFI_PROFILE:?Set RSAG_WIFI_PROFILE in /etc/rsag-wifi.env}"

if ! state=$(/usr/bin/nmcli -g GENERAL.STATE device show wlan0); then
    echo "check failed: could not read wlan0 state from NetworkManager" >&2
    exit 1
fi
profile=$(/usr/bin/nmcli -g GENERAL.CONNECTION device show wlan0 2>/dev/null || true)
case "$state" in
    100*)
        exit 0
        ;;
esac

echo "outage: wlan0 state=$state; active profile=${profile:-none}"
/usr/sbin/iw dev wlan0 link 2>&1 || true
/usr/bin/rfkill list wifi 2>&1 || true
echo "retry: activating configured Wi-Fi profile on wlan0"
if /usr/bin/nmcli --wait 90 connection up id "$RSAG_WIFI_PROFILE" ifname wlan0; then
    final_state=$(/usr/bin/nmcli -g GENERAL.STATE device show wlan0)
    echo "result: activation succeeded; wlan0 state=$final_state"
else
    result=$?
    final_state=$(/usr/bin/nmcli -g GENERAL.STATE device show wlan0 2>/dev/null || true)
    echo "result: activation failed (nmcli exit $result); wlan0 state=${final_state:-unknown}" >&2
    exit "$result"
fi
