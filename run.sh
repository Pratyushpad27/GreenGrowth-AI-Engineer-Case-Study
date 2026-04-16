#!/usr/bin/env bash
# Run the full Crescent Harbor Direct Filer pipeline against all 8 scenarios.
# Requires the mock Authority endpoint to be running on localhost:8080.
# Usage: ./run.sh [extra args passed to pipeline.py]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python -m filer.pipeline "$@"
