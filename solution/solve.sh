#!/bin/bash
# Golden solution: build the Go ingress proxy (standard library only, offline), install the
# controller to /opt/canary/bin, and bring the ingress up at 100 % app-blue.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
install -d -m 0755 /opt/canary/bin
( cd "$HERE/proxy" && GOCACHE=/tmp/canary-gocache GOFLAGS=-mod=mod CGO_ENABLED=0 \
    go build -trimpath -ldflags="-s -w" -o /opt/canary/bin/canary-proxy . )
install -m 0755 "$HERE"/canary/{canaryctl,engine.py} /opt/canary/bin/
install -m 0644 "$HERE"/canary/common.py /opt/canary/bin/
/opt/canary/bin/canaryctl up
