#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/project/scripts" "$TMP/bin"
cp "$ROOT/scripts/_local_env.sh" "$TMP/project/scripts/_local_env.sh"
cp "$ROOT/scripts/index_with_gpu_handoff.sh" "$TMP/project/scripts/index_with_gpu_handoff.sh"

cat >"$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker <%s>\n' "$*" >>"$FAKE_EVENTS"
if [[ "$*" == *"SELECT"* ]]; then
  printf '1\n'
fi
EOF

cat >"$TMP/bin/conda" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "env" && "${2:-}" == "list" ]]; then
  printf 'maple-chat %s\n' "$PWD"
  exit 0
fi
printf 'conda <%s>\n' "$*" >>"$FAKE_EVENTS"
EOF

cat >"$TMP/bin/gpu-manager" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'gpu-manager <%s>\n' "$*" >>"$FAKE_EVENTS"
exit 99
EOF

chmod +x "$TMP/bin/docker" "$TMP/bin/conda" "$TMP/bin/gpu-manager"

export PATH="$TMP/bin:$PATH"
export FAKE_EVENTS="$TMP/events"
export CONDA_BIN="$TMP/bin/conda"
export CONDA_ENV="maple-chat"
export DATABASE_URL="postgresql+asyncpg://fixture.invalid/maple_chat"
export POSTGRES_PASSWORD="fixture-password" # pragma: allowlist secret
export EMBEDDING_PROVIDER="remote"
export EMBEDDING_BASE_URL="http://127.0.0.1:8081/v1"
export INDEX_BATCH_SIZE="17"
export INDEX_LOCK_FILE="$TMP/index.lock"
export MAPLE_CHAT_LLM_MANAGER="$TMP/bin/gpu-manager"

"$TMP/project/scripts/index_with_gpu_handoff.sh" >"$TMP/output"

grep -Fq 'via remote provider' "$TMP/output"
grep -Fq 'conda <run --no-capture-output -n maple-chat maple-chat index --batch-size 17>' \
  "$FAKE_EVENTS"
if grep -q 'gpu-manager' "$FAKE_EVENTS"; then
  echo 'remote embedding unexpectedly invoked the exclusive GPU manager' >&2
  exit 1
fi

echo 'remote embedding handoff test passed'
