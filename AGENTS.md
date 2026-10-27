# AGENTS.md

## Cursor Cloud specific instructions

### Architecture Overview

QwenPaw is a single-process Python FastAPI application (port 8088) with an embedded React/Vite frontend (the "Console"). No external databases are required — all state is stored in local JSON files under `~/.qwenpaw/`.

### Services

| Service | How to run | Port | Notes |
|---|---|---|---|
| **Backend (FastAPI)** | `qwenpaw app` | 8088 | Core service; serves API + embedded console |
| **Console dev server** | `cd console && npm run dev` | 5173 | Only needed for frontend development; set `QWENPAW_CORS_ORIGINS=http://localhost:5173` on the backend |

### Python & Dependencies

- Python 3.10+ is required (`.python-version` says 3.10, supports up to 3.13).
- Install dev deps: `pip install -e ".[dev]"` from repo root.
- The `qwenpaw` CLI is installed to `~/.local/bin/` — ensure this is on `PATH`.

### Console Frontend

- Uses npm with `package-lock.json`.
- Build: `cd console && npm ci && npm run build`
- After building, copy output: `mkdir -p src/qwenpaw/console && cp -R console/dist/. src/qwenpaw/console/`
- This step is required before `qwenpaw app` can serve the web UI.

### Running the App

1. `echo "y" | qwenpaw init --defaults` — initializes working directory at `~/.qwenpaw/`. The `echo "y"` accepts the security notice non-interactively.
2. `qwenpaw app` — starts the FastAPI server on port 8088.
3. The app requires an LLM API key configured via the Console UI (Settings → Models) to actually chat. Without it, chat returns a 400 error, but the UI and all other functionality work fine.

### Lint & Pre-commit

- `pre-commit run --all-files` — runs all checks (black, flake8, pylint, mypy, prettier, etc.).
- If `core.hooksPath` is set, unset it first: `git config --unset-all core.hooksPath`.
- Console lint (`cd console && npx eslint src/`) has pre-existing errors in the repo; this is normal.

### Tests

- Run unit tests: `pytest -v tests/unit/ --ignore=tests/unit/providers/test_provider_manager.py`
- The `tests/unit/providers/test_provider_manager.py` file has several tests that hang indefinitely due to a pre-existing deadlock issue. Exclude this file when running the full unit test suite.
- Run via helper: `python3 scripts/run_tests.py -u` (but note the provider_manager hang).
- See `scripts/README.md` for more test runner options.
