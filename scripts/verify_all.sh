#!/usr/bin/env bash
set -euo pipefail

scripts/verify_g001.sh
maple-evaluate \
  --cases tests/evaluation/korean-rag-v1.jsonl \
  --predictions tests/evaluation/fixture-predictions-v1.json
scripts/verify_g002.sh
scripts/verify_g007_restore.sh
