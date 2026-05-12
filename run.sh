#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

if [ ! -d "$VENV" ]; then
    echo "ERROR: virtual environment not found. Run ./create_venv.sh first."
    exit 1
fi

source "$VENV/bin/activate"

# onnxruntime-gpu on WSL2: need nvidia pip-package libs on LD_LIBRARY_PATH
# and CUDA_VISIBLE_DEVICES set so onnxruntime can enumerate the device.
NVIDIA_LIB_DIR="$VENV/lib/python3.12/site-packages/nvidia"
for lib_dir in "$NVIDIA_LIB_DIR"/*/lib; do
    [ -d "$lib_dir" ] && export LD_LIBRARY_PATH="$lib_dir:${LD_LIBRARY_PATH:-}"
done
export LD_LIBRARY_PATH="/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

cd "$SCRIPT_DIR"
echo "Starting Seestar Lab at http://localhost:5000"
python app.py
