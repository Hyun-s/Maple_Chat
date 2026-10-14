# Development with Conda

## Environment ownership

Maple Chat uses the host Conda installation at `/home/hyuns/anaconda3` and the dedicated
environment `maple-chat`. The `base` environment is not modified.

```bash
scripts/setup_local.sh
```

Run tools without depending on shell activation:

```bash
conda run -n maple-chat ruff check src tests
conda run -n maple-chat ruff format --check src tests
conda run -n maple-chat mypy src
conda run -n maple-chat pytest -q tests/unit
conda run -n maple-chat scripts/verify_all.sh
```

`scripts/verify_all.sh` is the full gate: it runs the G001 compliance checks (secret scan,
`pip check`, `pip-audit`, `pip-licenses`, Ruff, Ruff format, MyPy, unit tests, compose config),
the fixed evaluation fixture, `verify_g002.sh`, and `verify_g007_restore.sh`. On this aarch64
host the known `nvidia-cusparselt-cu13 0.8.1 is not supported on this platform` wheel-tag false
positive no longer aborts it: `verify_g001.sh` now whitelists that single line the same way
`scripts/setup_local.sh` does, and only when the machine is actually `aarch64`.

The wrapper now stops one step later, at `pip-audit`, which reports four advisories in two
transitive packages: `multidict 6.7.1` (CVE-2026-104874, fixed in 6.9.1, pulled in by
`aiohttp`/`yarl`) and `urllib3 2.7.0` (PYSEC-2026-4175/4176/4177, fixed in 2.8.0, reachable only
through the audit tooling `pip_audit`/`detect-secrets`/`CacheControl`, not through the bot). The
environment pins neither package directly, so clearing this needs deliberate dependency bumps
rather than an environment edit. Until they land, run the equivalent checks individually: each
G001 step and `scripts/verify_g002.sh` (the behaviour and coverage gate) pass locally, and
`verify_g007_restore.sh` is the restore drill.

The Conda environment installs the OCR execution profile only. Embedding and reranking are not
loaded in-process: they come from the remote TEI sidecars, so no local model artifact is read or
cached by Maple Chat code, and no local model runtime is installed with it. OCR is fail-closed and
executes through the host's local Tesseract binary. `scripts/setup_local.sh` installs from
`environment.yml`, then checks that the OCR adapter (Pillow plus `pytesseract`) imports and that
`tesseract` is on `PATH`, without modifying `base`.

There is deliberately no Torch or CUDA wheel in the environment. It used to carry a pinned CUDA
PyTorch that only `setup_local.sh` itself imported, so nothing in `src`, `scripts`, or `tests`
needed it; the check was removed together with the pin. Importing every application module and
running the unit suite with `torch` blocked at the import hook proves the independence, and
`sentence-transformers`, which had been pulling Torch in through the retired `ml` extra, is not a
dependency of anything that remains.

`requirements/lock.txt` pins the verified G001 development dependency set. Update it only after
installing the intended dependency changes in `maple-chat`, rerunning the security gates, and
capturing `python -m pip list --format=freeze` without the local editable package.

Never place real tokens, connection strings, salts, or rights evidence in `environment.yml`,
`.env.example`, test fixtures, or command output.
