from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_compose() -> dict[str, object]:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_postgres_is_internal_and_has_no_host_port() -> None:
    compose = load_compose()
    services = compose["services"]
    postgres = services["postgres"]

    assert postgres["image"].startswith("pgvector/pgvector:0.8")
    assert "cap_drop" not in postgres
    assert "ports" not in postgres
    assert compose["networks"]["data-plane"]["internal"] is True


def test_role_services_use_hardened_container_defaults() -> None:
    compose = load_compose()
    services = compose["services"]

    for role in ("bot", "scheduler", "crawler-worker", "index-worker"):
        service = services[role]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["user"] == "10001:10001"
        assert "data-plane" in service["networks"]
        assert "dcm-model-plane" in service["networks"]
        assert "ports" not in service
    assert compose["networks"]["dcm-model-plane"] == {
        "external": True,
        "name": "dcm-model-plane",
    }


def test_crawler_defaults_to_fixture_only_mode() -> None:
    compose = load_compose()
    crawler_env = compose["services"]["crawler-worker"]["environment"]

    assert crawler_env["LIVE_CRAWL_ENABLED"] == "${LIVE_CRAWL_ENABLED:-false}"
    assert crawler_env["LIVE_CRAWL_APPROVAL_FILE"] == "${LIVE_CRAWL_APPROVAL_FILE:-}"


def test_role_services_receive_only_the_secrets_they_need() -> None:
    services = load_compose()["services"]

    assert "DISCORD_TOKEN" in services["bot"]["environment"]
    for role in ("scheduler", "crawler-worker", "index-worker"):
        assert "DISCORD_TOKEN" not in services[role]["environment"]
    assert "PII_HASH_SALT" not in services["scheduler"]["environment"]
    assert "PII_HASH_SALT" in services["bot"]["environment"]
    assert "PII_HASH_SALT" in services["crawler-worker"]["environment"]
    assert "PII_HASH_SALT" in services["index-worker"]["environment"]
    assert "NEXON_API_KEY" in services["bot"]["environment"]
    for role in ("scheduler", "crawler-worker", "index-worker"):
        assert "NEXON_API_KEY" not in services[role]["environment"]


def test_env_example_keeps_secret_values_empty() -> None:
    entries = {}
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        entries[key] = value

    for secret_name in (
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "PII_HASH_SALT",
        "POSTGRES_PASSWORD",
        "NEXON_API_KEY",
    ):
        assert secret_name in entries
        assert entries[secret_name] == ""


def test_local_postgres_override_is_loopback_only() -> None:
    override = yaml.safe_load((ROOT / "compose.local.yml").read_text(encoding="utf-8"))
    ports = override["services"]["postgres"]["ports"]

    assert ports == ["127.0.0.1:${POSTGRES_PORT:-55432}:5432"]


def test_ci_contains_all_g001_quality_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required_command in (
        "detect-secrets-hook",
        "pip-audit",
        "pip-licenses",
        "ruff check",
        "ruff format --check",
        "mypy src",
        "pytest -q tests/unit",
        "docker compose --profile app config --quiet",
    ):
        assert required_command in workflow


def test_local_qwen_compose_and_full_backfill_entrypoints_are_pinned() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    model_script = (ROOT / "scripts/start_local_model.sh").read_text(encoding="utf-8")
    backfill_script = (ROOT / "scripts/run_full_backfill.sh").read_text(encoding="utf-8")

    assert "LLM_BASE_URL=http://127.0.0.1:8001/v1" in env_example
    assert "docker-compose.qwen38-27b.yml" in model_script
    assert 'LLM_MODEL="${LLM_MODEL:-local-coder}"' in model_script
    assert "--scope full_backfill" in backfill_script
    assert "--resume" in backfill_script
    for board_id in (2294, 2295, 2296, 2297, 2298, 2300, 2304):
        assert str(board_id) in backfill_script


def test_remote_embedding_is_explicit_and_skips_exclusive_gpu_handoff() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = load_compose()
    index_handoff = (ROOT / "scripts/index_with_gpu_handoff.sh").read_text(encoding="utf-8")
    recovery = (ROOT / "scripts/recover_deleted_production_data.sh").read_text(encoding="utf-8")

    assert "EMBEDDING_PROVIDER=remote" in env_example
    assert "EMBEDDING_BASE_URL=http://127.0.0.1:8081/v1" in env_example
    assert "EMBEDDING_REMOTE_MODEL=bge-m3" in env_example
    for role in ("bot", "scheduler", "crawler-worker", "index-worker"):
        environment = compose["services"][role]["environment"]
        assert environment["EMBEDDING_PROVIDER"] == "${EMBEDDING_PROVIDER:-remote}"
        assert environment["EMBEDDING_BASE_URL"] == (
            "${EMBEDDING_BASE_URL:-http://dcm-embedding:80/v1}"
        )
        assert environment["EMBEDDING_REMOTE_MODEL"] == "${EMBEDDING_REMOTE_MODEL:-bge-m3}"
        assert environment["EMBEDDING_MODEL_COMMIT"].endswith(
            "5617a9f61b028005a4858fdac845db406aefb181}"
        )
    for script in (index_handoff, recovery):
        remote_branch = script.index('if [[ "$embedding_provider" == "remote" ]]')
        gpu_job_branch = script.index('elif [[ "$embedding_device" == "cuda"')
        assert remote_branch < gpu_job_branch
        assert "EMBEDDING_PROVIDER must be local or remote" in script


def test_integration_database_reset_requires_an_explicit_isolated_database_guard() -> None:
    verification = (ROOT / "scripts/verify_g002.sh").read_text(encoding="utf-8")
    fixture = (ROOT / "tests/integration/conftest.py").read_text(encoding="utf-8")

    assert "G002_ALLOW_DESTRUCTIVE_TEST_DATABASE=1" in verification
    assert 'os.environ.get("G002_ALLOW_DESTRUCTIVE_TEST_DATABASE") != "1"' in fixture
    assert "G002_DATABASE_URL matches DATABASE_URL" in fixture


def test_nexon_shell_exports_are_preserved_and_analytics_is_in_site_head() -> None:
    local_env = (ROOT / "scripts/_local_env.sh").read_text(encoding="utf-8")
    site = (ROOT / "Maple_chat_site/index.html").read_text(encoding="utf-8")

    assert "NEXON_API_KEY NEXON_ANALYTICS_SCRIPT" in local_env
    assert site.count("nexon-open-api-analytics:start") == 1
    assert site.count("nexon-open-api-analytics:end") == 1
    assert "https://openapi.nexon.com/js/analytics.js?app_id=" in site
    assert site.index("nexon-open-api-analytics:start") < site.index("</head>")


def test_daily_sync_uses_id_deduplication_and_continues_bounded_backfill() -> None:
    daily_script = (ROOT / "scripts/daily_sync_loop.sh").read_text(encoding="utf-8")
    incremental = (ROOT / "scripts/daily_incremental_sync.sh").read_text(encoding="utf-8")
    backfill_script = (ROOT / "scripts/run_full_backfill.sh").read_text(encoding="utf-8")
    sync_script = (ROOT / "scripts/sync_live.sh").read_text(encoding="utf-8")

    assert 'CRAWL_DAILY_AT="${CRAWL_DAILY_AT:-05:00}"' in daily_script
    assert "daily_incremental_sync.sh" in daily_script
    assert "maple-chat-daily-scheduler.lock" in daily_script
    assert "flock -n 8" in daily_script
    assert "daily_incremental_sync.sh" in sync_script
    assert "--skip-existing" in incremental
    assert "--preserve-backfill-checkpoint" in incremental
    assert "DAILY_LATEST_MAX_SCAN_PAGES" in incremental
    assert "continuing one backfill page at $next_page" in incremental
    assert 'for board in "${BOARDS[@]}"' in incremental
    assert "index_with_gpu_handoff.sh" in incremental
    assert "flock 8" in incremental
    assert "flock 9" in backfill_script


def test_official_patch_notes_have_a_weekly_thursday_scheduler_and_separate_lock() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    weekly = (ROOT / "scripts/weekly_patch_notes_loop.sh").read_text(encoding="utf-8")
    sync = (ROOT / "scripts/sync_patch_notes.sh").read_text(encoding="utf-8")
    bot = (ROOT / "scripts/run_live_bot.sh").read_text(encoding="utf-8")

    assert "PATCH_NOTES_WEEKLY_AT=05:00" in env_example
    assert "PATCH_NOTES_RETENTION_DAYS=365" in env_example
    assert "next_weekly_patch_run" in weekly
    assert "/tmp/maple-chat-patch-notes.lock" in weekly  # noqa: S108
    assert "/tmp/maple-chat-patch-scheduler.lock" in weekly  # noqa: S108
    assert "flock -n 8" in weekly
    assert "flock 9" in weekly
    assert "sync-patch-notes" in sync
    assert "--no-index" in sync
    assert "index_with_gpu_handoff.sh" in sync
    assert "weekly_patch_notes_loop.sh" in bot


def test_production_recovery_rebuilds_every_reproducible_data_layer() -> None:
    recovery = (ROOT / "scripts/recover_deleted_production_data.sh").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts/verify_recovery_state.py").read_text(encoding="utf-8")

    for board_id, target_page in {
        2294: 200,
        2295: 200,
        2296: 200,
        2297: 200,
        2298: 200,
        2300: 200,
        2304: 200,
    }.items():
        assert str(board_id) in recovery
        assert str(target_page) in recovery
    for required_step in (
        "sync-knowledge",
        "sync-patch-notes",
        "--no-index",
        "crawl-live",
        "maple-chat index",
        "gpu-job run",
        "verify_recovery_state.py",
        "BACKUP_INCLUDE_VECTORS=true",
        "RECOVERY_CRAWL_RETRIES",
        "retrying from its committed checkpoint",
        "resetting the consecutive retry counter",
    ):
        assert required_step in recovery
    for exact_graph_count in ("7", "217", "755", "292"):
        assert exact_graph_count in verifier
    assert "active_chunks != active_embeddings" in verifier
