FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      haproxy nginx-light iproute2 util-linux procps python3 python3-pytest golang-go curl jq libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl disable haproxy nginx 2>/dev/null || true
RUN groupadd -g 1500 canary && useradd -u 1500 -g 1500 -m -s /bin/bash canary \
 && groupadd -g 1600 apps && useradd -u 1600 -g 1600 -M -s /usr/sbin/nologin apps
COPY apps/app.py /opt/apps/app.py
COPY start-env.sh /usr/local/sbin/start-env
WORKDIR /root
ENTRYPOINT ["/usr/local/sbin/start-env"]
