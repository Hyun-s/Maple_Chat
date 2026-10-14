#!/usr/bin/env bash
set -euo pipefail

mapfile -t repository_files < <(git ls-files --cached --others --exclude-standard)
detect-secrets-hook --baseline .secrets.baseline "${repository_files[@]}"
pip_check_status=0
pip_check_output="$(python -m pip check 2>&1)" || pip_check_status=$?
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
fi
pip-audit
pip-licenses --format=markdown --with-urls >/dev/null
ruff check src tests migrations
ruff format --check src tests migrations
mypy src
pytest -q tests/unit
docker compose --profile app config --quiet
