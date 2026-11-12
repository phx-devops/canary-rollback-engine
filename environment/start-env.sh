#!/bin/bash
# Container entrypoint (root). Provides the network namespace and the two app targets.
set -e
MODE="${1:-}"
[ -e /run/netns/canary ] || ip netns add canary
nsenter --net=/run/netns/canary -- ip link set lo up
install -d -o apps -g apps -m 0700 /run/apps
for spec in "app-blue 1.0 9001" "app-green 2.0 9002"; do
  set -- $spec
  pkill -f "app.py --name $1 " 2>/dev/null || true
  nsenter --net=/run/netns/canary -- setpriv --reuid apps --regid apps --init-groups --inh-caps=-all \
    setsid -f python3 -I /opt/apps/app.py --name "$1" --version "$2" --port "$3" --admin "/run/apps/$1.sock" \
    </dev/null >>/var/log/apps-$1.log 2>&1
done
for n in app-blue app-green; do for i in $(seq 50); do [ -S /run/apps/$n.sock ] && break; sleep 0.1; done; done
# Record what /run and /tmp hold at container start: a simulated reboot wipes everything else there.
install -d -m 0700 /etc/canary-env
find /run /tmp -mindepth 1 -maxdepth 1 -print > /etc/canary-env/baseline.txt
chmod 0600 /etc/canary-env/baseline.txt
[ "$MODE" = "--no-wait" ] || exec sleep infinity
