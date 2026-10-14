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
host `pip check` aborts it on the known `nvidia-cusparselt-cu13 0.8.1 is not supported on this
platform` wheel-tag false positive. `scripts/setup_local.sh` already whitelists that one line;
`verify_g001.sh` does not, so the whole script stops at step two. This is a platform condition,
not a repository defect, and it reproduces on an unmodified checkout. To run the equivalent checks
now, execute the individual G001 steps and `scripts/verify_g002.sh` (the behaviour and coverage
gate, which passes locally).

The Conda environment installs the OCR execution profile and the pinned CUDA PyTorch that the
setup script verifies. Embedding and reranking are not loaded in-process: they come from the
remote TEI sidecars, so no local model artifact is read or cached by Maple Chat code. OCR is
fail-closed and executes through the host's local Tesseract binary. `scripts/setup_local.sh`
checks that CUDA is available to the pinned Torch build and that Tesseract is on `PATH`, without
modifying `base`.

`requirements/lock.txt` pins the verified G001 development dependency set. Update it only after
installing the intended dependency changes in `maple-chat`, rerunning the security gates, and
capturing `python -m pip list --format=freeze` without the local editable package.

Never place real tokens, connection strings, salts, or rights evidence in `environment.yml`,
`.env.example`, test fixtures, or command output.
