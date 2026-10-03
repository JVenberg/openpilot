#!/usr/bin/env bash
# Runs the launch-fix tests without the device toolchain; needs uv.
set -euo pipefail
cd "$(dirname "$0")"
uv run --no-project --python 3.12 \
  --with pytest --with hypothesis --with numpy --with pycapnp --with tqdm --with pycryptodome --with setproctitle --with zstandard --with pyzmq \
  pytest -q "$@"
