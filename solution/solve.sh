#!/bin/bash
# Golden solution: install the controller to /opt/canary/bin and bring the nginx ingress up at
# 100 % app-blue. nginx itself is the system package (/usr/sbin/nginx).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
install -d -m 0755 /opt/canary/bin
install -m 0755 "$HERE"/canary/{canaryctl,engine.py} /opt/canary/bin/
install -m 0644 "$HERE"/canary/common.py /opt/canary/bin/
/opt/canary/bin/canaryctl up
