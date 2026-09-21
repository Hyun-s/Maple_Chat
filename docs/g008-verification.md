# G008 Verification Evidence

## Automated code gate

The versioned `korean-rag-v1` fixture contains 110 Korean cases covering five job families,
general tips, Q&A, OCR, dated conflicts, no-evidence refusal, and prompt-injection attacks.

```text
Fixture Recall@10: 1.00 (required >= 0.85)
Fixture MRR@10: 1.00 (required >= 0.70)
Minimum family Recall@10: 1.00
Expected answer-term rate: 1.00
Unsupported answer rate: 0.00
Exact source match rate: 1.00
Security violations: 0
Ruff + format: passed
Mypy strict (43 source files): passed
Unit + PostgreSQL integration tests: 105 passed
Branch-aware coverage: 90.21% (required >= 90%)
Alembic upgrade/downgrade/upgrade: passed
Temporary-database restore drill: passed
Hardened application image build and configuration smoke test: passed
```

These are deterministic fixture/corpus regression metrics, not a claim about unapproved live
Inven content or a human-reviewed production corpus. The evaluator deliberately reports the
following independent release blockers until artifacts are supplied outside Git:

1. Inven written permission or appropriate legal-review evidence.
2. At least 100 human answer judgments meeting the contractual fidelity/accuracy criteria.
3. Private-guild and `/home/hyuns/local-claude-code` live validation evidence.

Code completion never overrides these blockers.

## 2026-09-02 Agent extension and live E2E regression

The `g010_agent_runs` extension added the bounded planner/tool loop, NEXON and local evidence tool
registries, durable run/tool checkpoints, and requester-scoped Discord resume. Fresh verification
used a temporary PostgreSQL container. The orchestration regression uses mock generators, and the
separate E2E run uses the real local Qwen, operational retrieval corpus, and Discord Gateway.

```text
Unit + PostgreSQL integration tests: 237 passed
Branch-aware coverage: 90.47% (required >= 90%)
Alembic upgrade/downgrade/upgrade through g010_agent_runs: passed
Ruff + format: passed
Mypy strict (62 source files): passed
Focused Agent/tool/routing unit tests: 23 passed
```

The live run additionally proved local SSE usage reporting, hybrid retrieval, graph augmentation,
Agent tool selection and persistence, Discord Gateway authentication, and a real user
mention-to-generated-reply path. See `docs/e2e-verification-2026-09-02.md` for the measurements and
remaining NEXON API gap.

The combined `verify_all.sh` wrapper currently stops at `python -m pip check` because the aarch64
Conda environment reports `nvidia-cusparselt-cu13 0.8.1` as unsupported on the current platform.
The secret scan, `pip-audit` (no known vulnerabilities), license inventory, Ruff, format, Mypy,
evaluation, Compose smoke test, PostgreSQL tests, migrations, and restore drill pass independently.
Do not report the combined wrapper itself as passing until the CUDA package metadata/lock is fixed.
