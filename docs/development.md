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

The Conda environment includes the ML and OCR execution profiles. Both adapters are fail-closed:
model loading uses an immutable revision with
`local_files_only=True`, and OCR executes through the host's local Tesseract binary. Production
model artifacts are cached locally. `scripts/setup_local.sh` verifies CPU-only Torch and the
Tesseract executable without modifying `base`.

`requirements/lock.txt` pins the verified G001 development dependency set. Update it only after
installing the intended dependency changes in `maple-chat`, rerunning the security gates, and
capturing `python -m pip list --format=freeze` without the local editable package.

Never place real tokens, connection strings, salts, or rights evidence in `environment.yml`,
`.env.example`, test fixtures, or command output.
