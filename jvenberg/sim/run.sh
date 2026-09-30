#!/usr/bin/env bash
# Runs the closed-loop sim checks; optional arg is a stock sunnypilot tree to compare against.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
STOCK="${1:+$(cd "$1" && pwd)}"

if [[ "$(uname -s)-$(uname -m)" =~ ^Linux-(aarch64|arm64)$ ]]; then
  exec uv run --no-project --python 3.12 \
    --with numpy --with pycapnp==2.1.0 --with casadi --with pyzmq --with setproctitle --with sentry-sdk \
    --with requests --with tqdm --with pycryptodome --with zstandard \
    python "$HERE/check.py" --patched "$ROOT" ${STOCK:+--stock "$STOCK"}
fi

# Prebuilt .so files are linux/arm64 only
docker build -q --platform linux/arm64 -t sp-sim "$HERE" >/dev/null
exec docker run --rm --platform linux/arm64 -v "$ROOT:/sp:ro" ${STOCK:+-v "$STOCK:/stock:ro"} sp-sim \
  /sp/jvenberg/sim/check.py --patched /sp ${STOCK:+--stock /stock}
