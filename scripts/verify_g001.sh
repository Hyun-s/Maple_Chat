#!/usr/bin/env bash
set -euo pipefail

mapfile -t repository_files < <(git ls-files --cached --others --exclude-standard)
detect-secrets-hook --baseline .secrets.baseline "${repository_files[@]}"
python -m pip check
pip-audit
pip-licenses --format=markdown --with-urls >/dev/null
ruff check src tests migrations
ruff format --check src tests migrations
mypy src
pytest -q tests/unit
docker compose --profile app config --quiet
