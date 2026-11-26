#!/bin/sh
# Start the gate from the installed image using its pinned Python environment.
set -eu

cd /opt/llm-ci-gate
# exec forwards signals directly to Python. -E ignores caller PYTHON* settings.
exec /app/.venv/bin/python -E /opt/llm-ci-gate/ci/gate/run.py "$@"
