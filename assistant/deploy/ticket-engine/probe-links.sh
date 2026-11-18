#!/bin/bash
set -u
DESK="${1:-fa-ticket-engine}"
# shellcheck disable=SC1090
. "/etc/$DESK.conf"
SECRET=/var/lib/fa-ticket-engine/probe-secret
TOKEN="PROBE-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
ID="fa-probe-$$"
LINK=$WORKSPACE/artifacts/$ID.md
cleanup() { sudo -n rm -f "$SECRET"; sudo -n -u "$ENGINE_USER" rm -f "$LINK"; }
trap cleanup EXIT
printf '%s\n' "$TOKEN" | sudo -n tee "$SECRET" >/dev/null && sudo -n chmod 600 "$SECRET"
sudo -n -u "$ENGINE_USER" ln -sf "$SECRET" "$LINK"
out="$(sudo -n "/usr/local/bin/$DESK" artifact "$ID" 2>&1)"; rc=$?
if printf '%s' "$out" | grep -q "$TOKEN"; then echo "FAIL: the bridge printed a root-only file through the engine user's link"; exit 1; fi
echo "ok: refused (exit $rc)"
