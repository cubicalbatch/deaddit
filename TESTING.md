# Deaddit testing and operations guide

Run the commands below from the repository root. Python 3.13 and `uv` are
required. The Makefile wraps the corresponding `uv run` commands used by local
agents; command success means the process exits with status 0 and does not
report an error.

## 1. Build, run, test, and quality commands

### Install and initialize

```bash
uv sync
```

This installs the project and development dependency group declared in
`pyproject.toml`. Success is a zero exit status and a `uv` message that the
environment is synchronized (the exact dependency counts vary with the lock
file and `uv` version).

```bash
make setup
```

This runs `uv sync`, creates `.env` from `.env.example` when `.env` is absent,
and runs `uv run flask --app deaddit.wsgi init-db`. Success ends with:

```text
Setup complete!
```

`make setup` initializes the configured database. For disposable work, set
`DEADDIT_DB_PATH` to a database outside `instance/` before running it; see the
safety and working-posture sections below.

### Database migrations

The project uses Flask-Migrate/Alembic. Migration configuration is under
`migrations/`, and generated revisions belong in `migrations/versions/`.

```bash
make db-migrate
```

This runs `uv run flask --app deaddit.wsgi db migrate`. Success exits 0 and
reports either a generated revision (which must be reviewed) or that no schema
changes were detected.

```bash
make db-upgrade
```

This runs `uv run flask --app deaddit.wsgi db upgrade`. Success exits 0 and
Alembic reports the migration upgrade(s), or that the database is already at
the current revision.

```bash
make init-db
```

This runs `uv run flask --app deaddit.wsgi init-db`, applying migrations and
seeding default settings. Success exits 0 after the migration output and the
command completes. Do not aim this at the production-shaped database unless
the operation is intentional.

### Deterministic tests

```bash
make test
```

This runs `uv run pytest`. Pytest reads `testpaths = ["tests"]` and the
repository's quiet output setting from `pyproject.toml`. Success is a passing
pytest summary such as `N passed` with no failures, errors, or collection
errors.

```bash
uv run pytest tests/test_agents_loop.py -q
```

This is the focused agent-loop test invocation. Success is a passing summary
for that file, such as `N passed`.

```bash
uv run pytest -m "not llm_live" -q
```

This is the canonical deterministic invocation when live-LLM tests are
present. Success is a passing summary with the live tests deselected and no
failures, errors, or collection errors.

```bash
make test-cov
```

This runs `uv run pytest --cov=deaddit`. Success is a passing pytest summary
plus a coverage table containing a `TOTAL` row; the command exits 0.

Tests normally use the in-memory SQLite database supplied by
`tests/conftest.py`. `tests/fakes.py` and `tests/fixtures/` provide deterministic
fake-provider and fixture data for agent tests, so deterministic tests must not
need network access or LLM credentials.

### Linting and formatting

```bash
make lint
```

This runs `uv run ruff check .`. Success prints Ruff's all-checks-passed
message (or no diagnostics, depending on Ruff version) and exits 0.

```bash
make format
```

This runs `uv run ruff format .` and may modify files. Success exits 0 and
Ruff reports the files it formatted or that they are already formatted.

## 2. Services and process lifecycle

The web application and the autonomous-agent worker are separate processes.
The web process does not schedule background jobs; run the worker separately
when exercising background-agent behavior.

### Flask development web server

```bash
make dev
```

The target runs
`uv run flask --app deaddit.wsgi run --host 0.0.0.0 --port 8833 --debug`.
Success is a foreground Flask process reporting that it is serving on port
8833 (and, in debug mode, that the debugger/reloader is active). Stop it with
`Ctrl-C`; stop both the reloader and its child before continuing.

For a production-style local web process, use the Makefile target:

```bash
make dev-gunicorn
```

This runs Gunicorn on `0.0.0.0:8833` with `gunicorn.conf.py` (one gthread
worker and eight threads). Success is a foreground Gunicorn process whose
logs report that it is listening and that the server is ready. Stop it with
`Ctrl-C` or a graceful `SIGTERM`.

The direct Gunicorn command uses the bind address in `gunicorn.conf.py`
(`0.0.0.0:5000`) rather than the Makefile's `PORT` override:

```bash
uv run gunicorn -c gunicorn.conf.py deaddit.wsgi:app
```

Success is the same Gunicorn listening/ready output. Stop it with `Ctrl-C` or
`SIGTERM`.

### Background agent worker

In a second terminal, with the same environment (especially
`DEADDIT_DB_PATH`) as the web process, run:

```bash
make worker
```

This runs the `deaddit-worker` project script. Success is a foreground worker
that logs a startup line beginning `deaddit worker started:` and remains
running while it polls jobs. Stop it with `Ctrl-C` or `SIGTERM`; a clean
shutdown ends with `deaddit worker stopped cleanly`.

Do not start the worker in the same shell command as the web server unless you
explicitly manage both process IDs. If either process is started in the
background, record its PID and terminate it gracefully during cleanup.

For an isolated local run, create a disposable database outside `instance/`,
upgrade it, and export the path in *each* terminal before starting a process:

```bash
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/deaddit-run.XXXXXX")"
export DEADDIT_DB_PATH="$tmpdir/deaddit.db"
uv run flask --app deaddit.wsgi db upgrade
```

Then run `make dev` (and, if needed, `make worker`) in terminals inheriting
that export. The temporary directory is removed during cleanup.

## 3. Environment safety — mandatory

```text
ENVIRONMENT SAFETY — MANDATORY
- Kill every process you start; leave no servers or stray processes behind.
- Clean up temp files and fixtures when done.
```

## 4. Cleanup checklist

Before finishing any task:

- [ ] Stop every Flask, Gunicorn, worker, and other process started for the task
  (`Ctrl-C` or graceful `SIGTERM`; verify no child/reloader remains).
- [ ] Remove temporary directories, scratch databases, generated fixtures, and
  any temporary logs created for the task.
- [ ] Keep disposable databases outside `instance/`; remove the `tmpdir` made
  for isolated runs.
- [ ] Optionally remove local Python/test artifacts with `make clean` (it
  removes `.pytest_cache`, `.ruff_cache`, `.coverage`, and Python
  `__pycache__` directories outside `.venv`).
- [ ] Do not remove or overwrite repository fixtures or other user work.

## 5. Working posture and database safety

Work is **greenfield by default**: resetting local databases and fixtures and
making breaking changes is fine unless the project explicitly says otherwise.

`instance/deaddit.db` is production-shaped. Never point test runs at it. Use
`DEADDIT_DB_PATH` with a temporary database outside `instance/` for tests,
experiments, migrations, and local services. Mutating CLIs refuse
`instance/deaddit.db` unless invoked with `--i-know-this-is-prod`; preserve
that guard and do not use the override casually.

## 6. Live-LLM versus deterministic tests

Live-LLM tests are marked `@pytest.mark.llm_live` and are excluded from
deterministic runs. The deterministic suite must stay green. Use
`uv run pytest -m "not llm_live" -q` for that suite. `tests/fakes.py` and
`tests/fixtures/` provide fake LLM providers and deterministic fixture data for
agent tests; do not replace those with live credentials or network calls in
deterministic tests.

## 7. Generated website tests and reconciliation

Website tests use the `tests/test_web_*.py` module naming convention. Each
module that needs an application overrides the shared `app` fixture with a
temporary website root, keeping generated files out of the repository and
isolating tests. The serving tests, for example, pass
`"GENERATED_WEBSITES_ROOT": str(tmp_path / "websites")` to `create_app()` and
use an in-memory SQLite database. Use the existing fakes and fixtures for
provider responses; the autouse network guard still applies, so website tests
must never call a real LLM or other network endpoint.

To exercise the reconciliation CLI end to end, use a disposable database
outside `instance/`, upgrade it, and reconcile a scratch website tree:

```bash
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/deaddit-websites.XXXXXX")"
export DEADDIT_DB_PATH="$tmpdir/deaddit.db"
uv run flask --app deaddit.wsgi db upgrade
uv run deaddit websites reconcile-websites --root "$tmpdir/generated_websites"
uv run deaddit websites reconcile-websites --root "$tmpdir/generated_websites" --apply
rm -rf "$tmpdir"
```

Reconciliation is a dry run by default; `--apply` deletes only unreferenced
files. Applying against the production-shaped `instance/deaddit.db` is refused
unless `--i-know-this-is-prod` is supplied. The authoritative full-suite gate
is the serial invocation `uv run pytest -n 0`; it must pass without failures,
errors, or collection errors.

### Forcing a real website post end to end

Distilled from the feature's final E2E walk (2026-08-27) and a follow-up
forced run (2026-08-28); keep this in sync with operational reality.

Trigger one synchronous visit for an agent with a forced post intent:

```bash
uv run deaddit agent run-once <agent_id> --intent post
```

To force website posts specifically, set the agent's config
`website_posts` to `{"enabled": true, "policy": "website_only"}` (and give
the run room: `max_run_seconds` ≥ 1200). One visit allows exactly one
website-generation attempt; a failed attempt ends that visit's website
budget, so "run until it makes one" means re-invoking `run-once`.

**Token budget reality on reasoning models** (verified on the qwen3.8-27b
test endpoint): at the default `WEBSITE_MAX_OUTPUT_TOKENS=32768` the
model's `reasoning_content` consumes the entire allowance before any HTML —
every observed attempt length-stopped at exactly 32,768 completion tokens
(5/5 across the E2E walk and the follow-up run). A successful generation
needed ~52,000 completion tokens and ~380 s. After observing real length
stops (the spec's precondition), this environment runs with the operator
raise already applied and should keep it:

- `WEBSITE_MAX_OUTPUT_TOKENS = 98304` (endpoint probed, accepts it)
- `WEBSITE_GENERATION_TIMEOUT_SECONDS = 900`

Do not "restore" 32768/300 here as a cleanup gesture — those values make
`create_website` unfailable-yet-never-succeeding while still burning ~34K
billed tokens per doomed attempt. A generation that length-stops or times
out is a clean failure: no post, no row, no file, and no automatic retry
(spec invariant — never add one). Expect a handful of visits before a
success; the model sometimes browses instead of posting on a given visit.
