#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
python -m pytest tests/test_stack_processor.py tests/test_comet_processor.py -v "$@"
