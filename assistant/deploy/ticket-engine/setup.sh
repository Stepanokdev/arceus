#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
need() { [ -n "${!1:-}" ] || { echo "$1: required (see the top of $0)" >&2; exit 2; }; }
need ENGINE_USER; need PROJECT; need ASSISTANT_NAME; need MIRROR
[[ "$ENGINE_USER" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || { echo "ENGINE_USER: a Linux user name" >&2; exit 2; }
U=$ENGINE_USER; H=/home/$U
DESK="${DESK:-}"; [[ -z "$DESK" || "$DESK" =~ ^[a-z0-9]+$ ]] || { echo "DESK: a-z, 0-9" >&2; exit 2; }
NAME=fa-ticket-engine${DESK:+-$DESK}; WS=$H/workspace${DESK:+-$DESK}
ASSISTANT_USER="${ASSISTANT_USER:-$(id -un)}"
UNIT=bulava-engine-$U
fill() { sed -e "s#@USER@#$U#g" -e "s#@HOME@#$H#g" -e "s#@PROJECT@#$PROJECT#g" -e "s#@NAME@#$ASSISTANT_NAME#g" "$1"; }
id "$U" >/dev/null 2>&1 || sudo useradd -m -s /bin/bash "$U"
sudo chmod 700 "$H"
if [ -z "$DESK" ]; then
  sudo -u "$U" -H bash /opt/bulava/engine/install.sh >/dev/null
  fill "$HERE/engine.conf" | sudo -u "$U" tee "$H/engine.conf" >/dev/null && sudo chmod 644 "$H/engine.conf"
  sudo install -o "$U" -g "$U" -m 644 "$HERE/ticket-runspec.json" "$H/ticket-runspec.json"
  sudo -u "$U" -H bash -c "umask 077; touch $H/engine.env"
fi
sudo -u "$U" -H bash -c "mkdir -p $WS/code $WS/artifacts $WS/briefs && cd $WS && { [ -d .git ] || git init -q -b main; } \
  && printf 'code/\nartifacts/\nbriefs/\n' > .gitignore"
sed -e "s/{project}/$PROJECT/g" -e "s/{name}/$ASSISTANT_NAME/g" "$HERE/AGENTS.md" | sudo -u "$U" tee "$WS/AGENTS.md" >/dev/null
sudo -u "$U" -H bash -c "cd $WS && git add .gitignore AGENTS.md && { git diff --cached --quiet || git -c user.name=assistant -c user.email=assistant@localhost commit -qm 'ticket desk'; }"
printf 'ENGINE_USER=%s\nENGINE_HOME=%s\nWORKSPACE=%s\nMIRROR=%s\nENGINE_BIN=/opt/bulava/engine/bin\nASSISTANT_USER=%s\n' \
  "$U" "$H" "$WS" "$MIRROR" "$ASSISTANT_USER" | sudo tee "/etc/$NAME.conf" >/dev/null
sudo install -o root -g root -m 755 "$HERE/fa-ticket-engine" "/usr/local/bin/$NAME"
if ! sudo -n -l -U "$ASSISTANT_USER" 2>/dev/null | grep -q "(ALL.*) NOPASSWD: ALL"; then
  rule="$ASSISTANT_USER ALL=(root) NOPASSWD: /usr/local/bin/$NAME"
  printf '%s\n' "$rule" > "/tmp/$NAME.sudoers" && sudo visudo -cqf "/tmp/$NAME.sudoers" \
    && sudo install -o root -g root -m 440 "/tmp/$NAME.sudoers" "/etc/sudoers.d/$NAME"; rm -f "/tmp/$NAME.sudoers"
fi
sudo "/usr/local/bin/$NAME" sync
sudo -u "$U" -H bash -lc "export PATH=$H/.local/bin:\$PATH; night-shift trust $WS >/dev/null; night-shift mcp-decide $WS skip >/dev/null || true"
if [ -z "$DESK" ]; then
  fill "$HERE/bulava-engine-tickets.service" | sudo tee "/etc/systemd/system/$UNIT.service" >/dev/null
  sudo chmod 644 "/etc/systemd/system/$UNIT.service"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$UNIT" >/dev/null
fi
echo "engine user $U ready; desk $NAME, workspace $WS, for $ASSISTANT_USER"
