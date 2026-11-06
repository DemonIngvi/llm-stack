#!/usr/bin/env bash
# Runs the bridge end-to-end test inside the dsh image: real dsh runtime, scripted model.
# Needs Docker; builds the image first if it is missing.
#
#   bash tests/bridge.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
docker image inspect llm-stack/dsh:local >/dev/null 2>&1 || docker build -q -t llm-stack/dsh:local "$ROOT/dsh"
docker run --rm -v "$ROOT":/src:ro --entrypoint python3 llm-stack/dsh:local /src/tests/bridge_e2e.py
