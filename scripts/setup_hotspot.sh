#!/bin/bash
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-config.yaml}"

if [[ $# -ge 2 ]]; then
  INTERFACE="${3:-wlan0}"
  CONNECTION_NAME="${4:-rtkbox-ap}"
  SSID="$1"
  PASSWORD="$2"
  ADDRESS="${5:-10.42.0.1/24}"
else
  mapfile -t AP_VALUES < <(
    /home/pi/zedf9p/.venv/bin/python - "$CONFIG_FILE" <<'PY'
import yaml
import sys
cfg = yaml.safe_load(open(sys.argv[1], "r", encoding="utf-8"))
ap = cfg.get("ap", {})
print(ap.get("interface", "wlan0"))
print(ap.get("connection_name", "rtkbox-ap"))
print(ap.get("ssid", "RTKbox"))
print(ap.get("password", ""))
print(ap.get("address", "10.42.0.1/24"))
PY
  )

  INTERFACE="${AP_VALUES[0]}"
  CONNECTION_NAME="${AP_VALUES[1]}"
  SSID="${AP_VALUES[2]}"
  PASSWORD="${AP_VALUES[3]}"
  ADDRESS="${AP_VALUES[4]}"
fi

if [[ -z "$SSID" ]]; then
  echo "SSID is required."
  exit 1
fi

if [[ ${#PASSWORD} -lt 8 ]]; then
  echo "Password must be at least 8 characters for WPA2."
  exit 1
fi

nmcli connection delete "$CONNECTION_NAME" >/dev/null 2>&1 || true

nmcli connection add type wifi ifname "$INTERFACE" con-name "$CONNECTION_NAME" autoconnect yes ssid "$SSID"
nmcli connection modify "$CONNECTION_NAME" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless-security.key-mgmt wpa-psk \
  802-11-wireless-security.psk "$PASSWORD" \
  ipv4.method shared \
  ipv4.addresses "$ADDRESS" \
  ipv6.method disabled \
  connection.interface-name "$INTERFACE"

nmcli connection up "$CONNECTION_NAME"

echo "Hotspot '$SSID' is up on $INTERFACE."
echo "Portal should be reachable at http://10.42.0.1:8080 once rtkbox portal is running."
