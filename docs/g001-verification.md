# G001 Verification Evidence

Verified on 2026-08-04 using the dedicated Conda environment `maple-chat` with Python 3.12.
No live Inven request or Discord connection was made.

## Automated gates

```text
detect-secrets: 0 baseline findings; repository hook passed
pip-audit: no known vulnerabilities (local unpublished package skipped)
pip-licenses: dependency license inventory generated successfully
ruff check: passed
ruff format --check: passed
mypy src: passed
pytest at G001 checkpoint: 21 passed, 98.87% branch-aware coverage
docker compose config --quiet: passed
```

## Deployment evidence

- `docker compose --profile app build bot` built `maple-chat-app:local` successfully.
- The built image validated configuration as UID/GID `10001:10001` with a read-only root
  filesystem, all Linux capabilities dropped, and `no-new-privileges` enabled.
- `postgres` has no Compose `ports` mapping and is attached only to the internal
  `data-plane` network.
- `crawler-worker` resolves `LIVE_CRAWL_ENABLED` to `false` unless explicitly overridden.
- Discord credentials are injected only into `bot`; the PII hash salt is injected into `bot` for
  durable requester pseudonyms and into the crawler/index workers for source pseudonyms.

## Safety evidence

- Missing or invalid required configuration reports only field names.
- Pydantic errors, `repr`, CLI output, and safe summaries do not expose token, database URL,
  or PII salt canaries.
- Live HTTP is denied by default and requires HTTPS, an exact allowlisted Inven host, a valid
  out-of-repository approval record, and a matching approved scope.
- `.env.example` contains empty values for every secret-bearing field.

The Inven rights gate and Discord public-release gate remain closed after G001 completion.
