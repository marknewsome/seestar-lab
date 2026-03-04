#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

if [ ! -d "$VENV" ]; then
    echo "ERROR: virtual environment not found. Run ./create_venv.sh first."
    exit 1
fi

source "$VENV/bin/activate"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

cd "$SCRIPT_DIR"
echo "Starting Seestar Lab at http://localhost:5000"
python app.py
