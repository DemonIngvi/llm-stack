#!/bin/sh
# dsh refuses wildcard binds, so listen on the container address and let
# Docker publish it on 127.0.0.1 of the host.
set -eu
ip="$(hostname -i | awk '{print $1}')"
exec dsh --profile web --no-open \
  --host "$ip" --port 3080 \
  --public-url "${DSH_PUBLIC_URL:-http://localhost:3081/}" \
  --trusted-host localhost --trusted-host 127.0.0.1
