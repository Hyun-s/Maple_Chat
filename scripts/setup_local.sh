#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_BIN="${CONDA_BIN:-/home/hyuns/anaconda3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-maple-chat}"

cd "$ROOT_DIR"
if "$CONDA_BIN" env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
  "$CONDA_BIN" env update -n "$CONDA_ENV" -f environment.yml --prune
else
  "$CONDA_BIN" env create -f environment.yml
fi

ENV_PYTHON="$("$CONDA_BIN" run -n "$CONDA_ENV" python -c 'import sys; print(sys.executable)')"

pip_check_status=0
pip_check_output="$("$ENV_PYTHON" -m pip check 2>&1)" \
  || pip_check_status=$?
if (( pip_check_status != 0 )); then
  unexpected_check_output="$(
    printf '%s\n' "$pip_check_output" \
      | grep -v -x 'nvidia-cusparselt-cu13 0.8.1 is not supported on this platform' \
      || true
  )"
  if [[ "$(uname -m)" != "aarch64" || -n "$unexpected_check_output" ]]; then
    printf '%s\n' "$pip_check_output" >&2
    exit "$pip_check_status"
  fi
  echo "Ignoring known pip-check SBSA wheel-tag false positive for nvidia-cusparselt-cu13 0.8.1"
else
  printf '%s\n' "$pip_check_output"
fi

"$ENV_PYTHON" - <<'PY'
import shutil

import torch

assert torch.cuda.is_available(), "CUDA Torch is required for BGE embeddings"
assert shutil.which("tesseract"), "Tesseract is missing from the Conda environment"
print(f"Conda runtime ready: CUDA Torch on {torch.cuda.get_device_name(0)} and Tesseract detected")
PY
