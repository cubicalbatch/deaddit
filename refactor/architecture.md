# Deaddit Refactor — Architecture & Code Health Plan

Owner: Architecture Lead (`ArchLead`) · Status: draft for orchestrator review · Date: 2026-08-24

## TL;DR

Deaddit is a 7,900-line monolith with a module-level Flask singleton, import-order-sensitive
wiring, no migrations, no tests, and a generation pipeline that talks to itself over HTTP.
This plan makes the code structurally sound **beneath** the four feature leads' work:

1. **App factory + explicit imports** replace the `app` singleton and star imports — the
   prerequisite for any test at all.
2. **Kill self-HTTP ingest** (both `loader.ingest()` and `jobs._execute_create_post`) in favor
   of a transactional service layer; `/api/ingest` survives as a thin public wrapper.
3. **Alembic migrations + SQLite WAL + composite indexes** replace `db.create_all()` — Platform
   Dynamics cannot add Vote tables without this.
4. **One dedicated worker process** running the existing APScheduler code, instead of a scheduler
   implicitly started inside every web process; `gevent` is dead weight and gets deleted.
5. **Test pyramid with a fake LLM provider**, GitHub Actions CI, packaging truth (uv.lock today
   omits `APScheduler` and `flask-socketio` — a fresh `uv sync` produces a broken app),
   env/DB config split, and repo hygiene.
Strangler throughout: every phase leaves the app runnable against the live 83MB database.

## Current State

### Application wiring: module-level singleton, order-sensitive imports

- `deaddit/__init__.py:9` creates `app = Flask(__name__)` at import time; `db = SQLAlchemy(app)`
  (:16), `cache = Cache(app)` (:17), `socketio = SocketIO(...)` (:18-28) are all module globals.
- `with app.app_context(): db.create_all()` runs at **import time** (`__init__.py:81-88`), along
  with `Config.initialize_defaults()` and `restart_pending_jobs()` (:96-101). Importing the
  package has side effects: it creates tables, seeds settings, and re-enqueues pending jobs.
- Wiring depends on import order: `from .config import Config  # noqa: E402` (:63) after app
  creation; routes register as side effects of `from .api import *` and
  `from .routes import *  # noqa: E402,F403` (:127-128). Neither `api.py` nor `routes.py`
  defines blueprints — their view functions decorate the global `app` directly
  (`deaddit/api.py:27`, `deaddit/routes.py:16`). Only `admin.py` uses a blueprint
  (`admin_bp = Blueprint("admin", __name__, url_prefix="/admin")`, `admin.py:40`,
  registered at `__init__.py:131`).
- Auth is a global `@app.before_request` hook (`authenticate()`, `__init__.py:44-59`) that
  string-matches `request.path.startswith("/api/ingest")` and admin paths, checking a bearer
  token from `API_TOKEN` (env first, DB later). There is no per-route auth declaration.
- `socketio` runs `async_mode="threading"`, polling-only (`transports=["polling"]`,
  `allow_upgrades=False`, `__init__.py:21-27`) — fine for an admin progress panel, and important
  context for the worker decision below.

### loader.py (~3,179 lines): engine, heuristics, HTTP, parsing, CLI in one file

Verified outline (symbol → line):

- Selection strategies: `select_subdeaddit_{weighted,round_robin,improved_random}` (:54,:107,:153),
  `select_user_*` (:262,:315,:361), dispatchers `select_subdeaddit_smart`/:193,
  `select_user_smart`/:401.
- Dead-weight test harnesses living in production code: `test_subdeaddit_distribution`/:426,
  `test_user_distribution`/:522 (plus click commands `test_user_dist`/:3155,
  `test_sub_dist`/:3168).
- LLM plumbing: `get_api_base_url`/:620, `send_request`/:747, `parse_data`/:906
  (regex-based JSON extraction), `select_model`/:646, `get_dynamic_temperature`/:690.
- **Self-HTTP ingest**: `ingest(data, type)` (:1031) does
  `requests.post(f"{get_api_base_url()}/api/ingest", ...)`; called by `create_post` (:1526),
  `create_subdeaddit` (:1596), `create_comment` (:2831). `create_post_with_replies` (:2946) even
  GETs its own read API (`requests.get(...)`) to find context posts.
- Realism heuristics: `calculate_realistic_upvotes`/:1909,
  `get_diverse_comment_strategy`/:1986, `analyze_conversation_context`/:2183,
  `get_varied_comment_structure`/:2331, reply-target selection :2492–:2676.
- Prompt builders: `get_system_prompt`/:1226, `get_post_prompt`/:1361,
  `get_enhanced_comment_prompt`/:1600, `analyze_community_culture`/:1298.
- Click CLI from :3012 (`cli` group with `subdeaddit/user/post/comment/loop/test_*_dist`).
- Live bug evidencing the tangle: `loader.py:621`
  reads `Config.get("get_api_base_url()", ...)` — a key literally named after the function — so it
  always falls back to `"http://localhost:5000"`.

### jobs.py (~1,685 lines): scheduler + job executor + second LLM client

- Module-level `scheduler = BackgroundScheduler(...)` (`jobs.py:37`) with `MemoryJobStore` and
  three `ThreadPoolExecutor(max_workers=1)` lanes (`default/high_priority/low_priority`, :30-34).
- `create_job` (:83) writes a `Job` row then `scheduler.add_job(execute_job, "date", ...)`;
  `start_scheduler()` (:69) runs lazily inside whichever process called `create_job` — i.e., the
  web process (admin triggers via `admin.py:25` importing `create_job`) **and** at import time via
  `restart_pending_jobs()` (`__init__.py:97-101`, defined `jobs.py:1620`).
- Job execution duplicates loader functionality rather than sharing it:
  `_send_openai_request` (:598) and `_parse_json_response` (:688) are a second, independent LLM
  client; `_generate_post_data`/:819, `_generate_comment_data`/:953, `_generate_user_data`/:507.
- The jobs path **also** self-HTTP-ingests: `_execute_create_post` builds
  `{"posts": [clean_post_data]}` and does `requests.post(f"{get_api_base_url()}/api/ingest", ...)`
  (verified at `jobs.py:1287-1293`).
- Restart/resume exists but is crude: `restart_pending_jobs()` (:1620) reschedules all
  `JobStatus.PENDING` rows on boot. Jobs that were mid-flight when the process died stay `RUNNING`
  forever (no timeout sweeper); there is no heartbeat or lease column on `Job`.

### Background execution vs. deployment reality (conflicts verified)

- `gunicorn.conf.py`: `worker_class = "sync"`, `workers = 2`, `preload_app = True`. So:
  - `preload_app=True` means `import deaddit` happens once in the master **before fork**;
    `restart_pending_jobs()` therefore starts scheduler threads in the master, and **threads do
    not survive `fork()`** — workers inherit a `scheduler.running == True` corpse, and
    `start_scheduler()`'s `if not scheduler.running` guard refuses to revive it.
  - Even without preload, 2 sync workers each run their own `MemoryJobStore`-backed scheduler
    over the same `Job` table: duplicate job execution and lost schedule state across restarts.
  - `gevent>=25.5.1` is pinned in `pyproject.toml:19` but nothing monkey-patches and no server
    uses it — it's inert weight (and a greenlet compile cost in Docker).
- **The Docker image doesn't even use gunicorn**: `Dockerfile` ends with
  `CMD ["python", "app.py"]`, and `app.py` runs `app.run(host="0.0.0.0", debug=True)` — the
  Flask dev server with the debugger enabled is what ships to production
  (`docker-compose.yml` runs `image: cubicalbatch/deaddit` from this Dockerfile).
- `wsgi.py` and `gunicorn.conf.py` exist on disk but are **gitignored**
  (`.gitignore` last lines: `gunicorn.conf.py`, `wsgi.py`; confirmed absent from `git ls-files`)
  — the deployment configs aren't version-controlled at all.
- `docker-compose.yml` passes no `environment:` block, so `API_TOKEN` never reaches the container;
  combined with `__init__.py:38-41`, deployments silently start with publicly-writable ingest.

### Self-HTTP ingest endpoint

- `/api/ingest` (`api.py:27-179`) and `/api/ingest/user` (:326) are decorated
  `@production_disabled` (`utils.py:17-36`: returns 404 when the DB setting `PRODUCTION=true`),
  plus bearer-token auth from `__init__.py:44-59`.
- `deaddit/data/load_seed_data.py` hardcodes `http://localhost:5000/api/ingest[/user]` and
  requests-POSTs seed users/subdeaddits through the public API — a third self-HTTP consumer.

### Config: DB-first hybrid with secrets in the database

- `Config.get` (`config.py:47-88`): DB `Setting.get_value(key)` → env var → `DEFAULTS`. Every
  call hits the database (no caching); `Config.get` is called in hot paths (template
  context processor `__init__.py:67-78`, per-request in `routes.py`).
- Secrets live in the DB: `DEFAULTS` includes `OPENAI_KEY`, `SECRET_KEY`
  (`"dev-secret-key-change-in-production"`, `config.py:24`), `API_TOKEN`
  (:23, default `None`); `initialize_defaults()` (:158-169) writes them into `Setting` rows.
  Anyone with the DB file has every secret; the 83MB `instance/deaddit.db` is exactly such a file.
- `SECRET_KEY` is loaded back into the app at import time (`__init__.py:84`).

### Data layer

- Models (`models.py`): `Subdeaddit` PK `name` (:8), `User` PK `username` (:58), `Post` (:20)
  and `Comment` (:39) with integer PKs. Single-column indexes already exist on
  `Post.subdeaddit_name/user/created_at/model/post_type` and `Comment.post_id/parent_id/user/
  created_at/model`. No composite indexes; no `Vote`/karma model anywhere.
- Feed query anti-pattern: `routes.index` (`routes.py:16-98`) runs
  `Post.query.order_by(func.random()).all()` — loads **every post row** into Python, then
  `paginate_posts_with_model_cycling` (`utils.py:98-144`) slices page 20 in memory. Cost grows
  linearly with total posts per page view, forever.
- No migrations: schema changes only via `db.create_all()` (`__init__.py:82`), which never alters
  existing tables. The live DB is ~83.5MB (`instance/deaddit.db`, plus an 80.5MB
  `deaddit.db.backup`). No WAL pragmas configured anywhere.

### Packaging drift (verified)

| Artifact | Problem |
|---|---|
| `pyproject.toml:13-20` | deps omit `apscheduler`, `click`, `flask-socketio` — all imported at runtime (`jobs.py:14-16`, `loader.py:9`, `__init__.py` socketio) |
| `requirements.txt` | omits `gevent` (pinned in pyproject); used by the Docker build |
| `uv.lock` | contains neither `flask-socketio` nor `apscheduler` (checked lock names) — `uv sync` yields an app that crashes on import |
| `pyproject.toml:9` | `requires-python = ">=3.9"` while stale pycache shows 3.12/3.13 use and ruff targets py39 |
| Docker | installs from `requirements.txt`, so pyproject deps are decorative |

### Repo hygiene

- Git-tracked tree is actually clean (49 files; `git ls-files` shows no caches/logs/db files).
  Junk lives **untracked on disk**: root `__pycache__/` with stale test bytecode of deleted test
  modules (`test_routes.cpython-313-pytest-8.4.0.pyc`, `test_admin…`, `test_enhanced_comments…`),
  `deaddit/__pycache__/` containing bytecode of deleted agent modules (`agent_engine`,
  `agent_actions`, `agent_analytics`, `agent_error_handler`, `admin_api` — sources gone,
  `deaddit/agents/` holds only `__pycache__/`), `app.log` (17.8KB), `flask.log`, empty `data.db`,
  `.pytest_cache/`, `.ruff_cache/`, `.claude/`, `building/`.
- `.gitignore` bugs: it ignores the two deployment files that should be tracked
  (`gunicorn.conf.py`, `wsgi.py`); the broad `lib/` pattern would ignore any source dir named
  `lib/`; `*.db` globally is fine but undocumented intent.
- No log rotation: `app.log`/`flask.log` grow unbounded; logging config is one line
  `logging.basicConfig(level=logging.WARNING)` (`__init__.py:34`) plus scattered `loguru` usage
  (`loader.py:11`, `jobs.py:18`) — two logging systems side by side.
- No backup automation: `deaddit.db.backup` is a year-old manual copy; docker-compose persists
  only the named volume `deaddit_data:/app/instance`.

### Tests & CI

- None. Only stale pytest bytecode remains (root `__pycache__/test_*.pyc`). Ruff is configured
  (`pyproject.toml:28-61`, select E/W/F/I/B/C4/UP) but nothing runs it automatically.

## Target State

### Package layout (strangler-compatible)

New packages appear alongside old modules; old modules re-export/delegate until each phase's
cutover deletes them. No big-bang rename: every phase keeps `deaddit` importable and the app
runnable.

```
deaddit/
├── __init__.py            # create_app() factory ONLY (no logic, no create_all)
├── extensions.py          # db, cache, socketio instances (no app binding; init_app pattern)
├── config.py              # NEW static env config (dataclass) — see Config split below
├── settings/              # DB-backed runtime Settings service (moved Config.get/set)
│   └── service.py
├── models/                # models.py split; same table names, no data migration needed
│   ├── __init__.py        # re-exports all models
│   ├── content.py         # Subdeaddit, Post, Comment
│   ├── user.py            # User
│   └── jobs.py            # Job (+ new lease/heartbeat columns)
├── domain/                # pure-ish business logic, no Flask request/response objects
│   ├── selection.py       # select_* strategies from loader.py:54-424
│   ├── realism.py         # calculate_realistic_upvotes etc. (loader.py:1909-2676)
│   ├── personas.py        # get_personality_archetype, archetype prompts (loader.py:1174-1360)
│   └── prompts.py         # prompt builders (loader.py:1226-1908) [LLM Lead co-owns]
├── services/              # content persistence — Resolution 1 canonical home
│   └── content.py         # create_post/comment/user/subdeaddit: the only write path
├── llm/                   # thin adapter now: send_request/parse_data moved verbatim;
│   └── client.py          # LLM Lead replaces internals with provider layer behind this seam
├── runtime/               # background execution (see worker section)
│   ├── scheduler.py       # standalone APScheduler entrypoint (moved jobs.py core)
│   └── jobtypes.py        # _execute_create_* functions
├── web/
│   ├── __init__.py        # register_blueprints(app)
│   ├── routes.py          # public pages (Blueprint "web")
│   ├── admin/             # admin_bp split from admin.py by concern (dashboard/content/settings/api-config)
│   ├── templates/         # unchanged location (templates/ stays package-relative)
│   └── static/
├── api/
│   └── v1.py              # Blueprint "api": read endpoints + /ingest thin wrapper
├── cli.py                 # click group moved from loader.py:3012+
└── data/                  # seed JSON + loader rewritten onto the content service
```

**Import graph rules (enforced by CI, see testing section):**

1. Allowed dependency direction: `models ← domain ← {runtime, web, api, cli}`; everything may
   import `extensions`, `config`, `settings`. Nothing in `domain/`/`llm/` imports Flask request /
   session / `current_app` (app context is allowed via `flask.current_app`-free design: services
   take explicit arguments; DB session comes from Flask-SQLAlchemy scoped session inside
   `app.app_context()` managed by callers).
2. No cycles: `domain` must not import `runtime`, `web`, `api`, or `cli`. `runtime` must not
   import `web`.
3. No `import *` anywhere; ruff rule F403/F405 added to the select list to enforce.
4. Blueprints only: view functions attach to Blueprints; `create_app` registers them. No
   `@app.route` outside `web/`/`api/`.
5. No module-level side effects beyond constructing extension objects; no I/O at import time
   (no `db.create_all()`, no scheduler start, no `Config.initialize_defaults()`).

```mermaid
graph TD
    cli[cli.py] --> domain
    api[api/v1.py] --> domain
    web[web/*] --> domain
    runtime[runtime/scheduler.py] --> domain
    domain --> llm[llm/client.py]
    domain --> models[models/*]
    models --> ext[extensions.py]
    subgraphs ok
end
```

### Killing self-HTTP ingest: service layer + transactional boundaries

`deaddit/services/content.py` owns row creation — canonical home and naming per master
roadmap Resolution 1 (AgenticCore and Dynamics build against these signatures). Extracted
from `api.py:27-373` body logic:

```python
# module-level functions; `created_at` override serves Dynamics' time-travel seeding
def create_post(data: dict, created_at: datetime | None = None) -> Post: ...
def create_comment(data: dict, created_at: datetime | None = None) -> Comment: ...
def create_user(data: dict) -> User: ...
def create_subdeaddit(data: dict) -> Subdeaddit: ...
```

- One function = one transaction (`db.session.add(...)` + single commit per batch, rollback on
  validation error), returning created ORM objects so callers get real IDs (today
  `loader.create_post` parses IDs out of the JSON HTTP response, `loader.py:1526-1531`).
- Callers migrated: `loader.create_post/create_subdeaddit/create_comment` (:1444,:1560,:2677),
  `loader.ingest()`/:1031 (deleted), `jobs._execute_create_post/_execute_create_comment` HTTP
  calls (`jobs.py:1287ff`), CLI, and `data/load_seed_data.py`.
- **`/api/ingest` stays as a thin wrapper during migration only** (`api/v1.py`): parse JSON →
  call the content service → serialize. Zero duplicated persistence logic; keeps
  `@production_disabled` semantics and moves auth off the string-matching `before_request`
  onto the blueprint (decorator or `before_request_blueprint`). The wrapper itself is
  **deleted at the Wave 6 cutover** (owner decision 8) together with AgenticCore D2 —
  README/API-doc references go with it.

### Data layer: Alembic + pragmas + indexes

- **Flask-Migrate (Alembic)** wired into `create_app`; `db.create_all()` deleted. First migration
  is a baseline: `flask db init && flask db migrate -m "baseline"` autogenerates from current
  models, then `flask db stamp head` on the live 83MB DB so history starts without touching data.
  Every subsequent schema change (Platform Dynamics votes, Agentic Core memory tables, Job lease
  columns) goes through migrations.
- **SQLite pragmas** via a `sqlalchemy.connect` event listener in `extensions.py`:
  `PRAGMA journal_mode=WAL` (readers don't block the writer during feed rendering while agents
  generate), `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`, `PRAGMA synchronous=NORMAL`.
- **Composite indexes** added by migration (matching real query shapes):
  - `post (subdeaddit_name, created_at DESC)` — community feeds (`routes.py:99+`);
  - `post (model, created_at DESC)` — model-filtered lists (`routes.py:57`, admin content);
  - `comment (post_id, created_at)` — thread loading (`api.build_comment_tree`, `api.py:297`);
  - `job (status, priority)` — `restart_pending_jobs` scan (`jobs.py:1626`).
- **Feed pagination rewrite** (pairs with UX Lead's feed work): replace
  `order_by(func.random()).all()` + Python slicing (`routes.py:55-56`,
  `utils.paginate_posts_with_model_cycling:98`) with SQL-side `LIMIT/OFFSET` on an indexed
  ordering. True random-feeds can be served from a cached candidate set (Flask-Caching already
  present, `__init__.py:13-14`) refreshed periodically by the worker — never O(table) per request.
- **When SQLite stops being enough** (stated criterion, not vibes): single-box SQLite+WAL is
  comfortable up to roughly low-GB file size and a handful of concurrent writers. The trigger to
  move to Postgres is the **agentic runtime, not human traffic**: if agent concurrency produces
  sustained multi-writer contention (recurring `database is locked` past busy_timeout under
  normal operation) or memory/history tables grow past ~2GB, switch. Because all access flows
  through SQLAlchemy ORM models and Alembic (never raw SQLite features), the switch is a config
  change plus a migration platform swap, planned *after* agentic-core lands and is measured —
  not preemptively.

### Background execution: one dedicated worker process

Options considered:

1. **Status quo** — APScheduler inside the web process. Rejected: broken under
   `preload_app=True` (threads die at fork, `gunicorn.conf.py`), duplicated per worker with a
   `MemoryJobStore`, and couples deploy/restart of the UI to killing in-flight LLM jobs.
2. **Redis-backed queue (RQ/dramatiq/arq)** — proper retries/visibility timeouts, but adds a
   broker service to a single-box self-hosted product. Premature today; revisit only if agentic
   runtime outgrows option 3 (the DB-backed `Job` table already provides queue semantics).
3. **Dedicated worker process, same stack** — a separate `deaddit-worker` entrypoint running the
   existing APScheduler code unchanged, with the `Job` table as the durable queue. **Chosen.**

Design:

- `deaddit-worker` console script (declared in `pyproject [project.scripts]`) runs
  `runtime/scheduler.py`: `start_scheduler()` + `restart_pending_jobs()` + blocking wait on
  SIGTERM/SIGINT → `scheduler.shutdown(wait=True)` (`jobs.py:69-81` logic relocated).
- **Claim protocol prevents double-execution** once web and worker are separate processes:
  `create_job` (called from web/admin/CLI) only inserts the `Job` row (status PENDING); the worker
  claims with `UPDATE job SET status='RUNNING', claimed_at=now, worker_id=? WHERE id=? AND
  status='PENDING'` and checks rowcount — atomic claim, no Redis needed. New columns
  `claimed_at`, `worker_id`, `heartbeat_at` on `Job` via Alembic (this plan's first non-baseline
  migration; coordinates with Agentic Core Lead who will extend `Job.parameters`).
- **Crash recovery**: worker startup sweeps `status='RUNNING' AND heartbeat_at < now()-X minutes`
  back to PENDING (generalizes existing `restart_pending_jobs`, `jobs.py:1620`); job executors
  bump `heartbeat_at` between items so long batches resume rather than replay. This gives
  "graceful shutdown/resume of agent runs" semantics before Agentic Core's runtime arrives —
  their agent-loop runner can adopt the identical claim/heartbeat contract per agent session.
- **Concurrency budgets**: keep the three named lanes (default/high_priority/low_priority,
  `jobs.py:30-34`) as the knob; agentic runtime later maps agent-session pools onto additional
  executors. Long LLM waits are just threads parked on `requests` — ThreadPoolExecutor handles
  hundreds; raise `max_workers` per lane rather than adding infrastructure.
- **Websocket progress** keeps working across processes: `_emit_job_update` (`jobs.py:273`)
  currently emits from the executing thread; once jobs run in the worker process, the worker
  emits status purely via the `Job` row (progress fields already persisted, `models.Job`) and the
  web process pushes updates — simplest correct version: admin JS polls the existing
  `/admin/api/jobs/<id>/status` endpoint (`admin.py:582`) on an interval; flask-socketio emission
  from the web process is dropped unless UX Lead's admin redesign wants push (their call; the
  socket.io server stays for interactive features either way since it lives in web only).

**gevent verdict: delete it.** Verified unused: no `monkey.patch_all()` anywhere, gunicorn runs
`worker_class="sync"`, socketio is `async_mode="threading"` polling-only (`__init__.py:21-27`).
Removing `gevent>=25.5.1` from `pyproject.toml:19` drops a heavy native dep. If real-time push is
ever needed at scale, SSE under threading workers is the boring next step, not gevent.

**Deployment target after this phase:** `gunicorn -c gunicorn.conf.py deaddit.wsgi:app` for web
(fix `CMD` away from `python app.py` debug server; track the config files in git) +
`deaddit-worker` as a second compose service sharing the same volume. Two processes, one box,
still boring.

### Testing strategy (first meaningful pyramid)

Layers, all runnable via plain `pytest`:

1. **Unit (fast, no DB)**: `domain/selection.py` strategies (seeded `random` — they already take
   counts/lists as inputs, `loader.py:54-424`), `parse_data` regex extraction
   (`loader.py:906`), `realism.py` heuristics with fixed inputs, prompt builders snapshot-tested
   loosely (assert structure/keys, not prose). Target the extracted modules; tests written
   against `loader.py` directly would be throwaway.
2. **Service integration (tmp SQLite)**: content-service (`services/content.py`) round-trips (validation errors, ID
   return, comment threading), settings service, job claim protocol (two fake claimants race one
   row — asserts single winner). App fixture uses `create_app(config={"SQLALCHEMY_DATABASE_URI":
   "sqlite:///file:test?mode=memory&uri=true", "TESTING": True})` — in-memory shared-cache DB, no
   disk fixture management.
3. **Smoke (one test boots the whole app)**: `create_app()` → `app.test_client().get("/")` and
   `/api/posts`, asserting 200 and setup-page behavior both empty and seeded DB. This is the test
   that would have caught the `loader.py:621` `Config.get("get_api_base_url()")` bug.
4. **Fake LLM provider**: `tests/fakes.py` implements the `llm/client.py` interface
   (whatever `send_request`/provider layer exposes — contract owned jointly with LLM Integration
   Lead) returning canned completions/tool-calls from fixture JSON. All job/service tests inject
   it; zero network in CI. A single optional integration marker (`pytest -m llm_live`) hits a real
   endpoint, deselected by default.

Fixture strategy: `conftest.py` provides `app`, `db_session`, `client`, `fake_llm`,
`seeded_db` (a few users/subdeaddits/posts via the content service itself — dogfooding). No
production-DB copies in tests.

CI (GitHub Actions, `.github/workflows/ci.yml`):

```yaml
# essence — two jobs on push/PR
lint:   uv sync && uv run ruff check . && uv run ruff format --check .
test:   uv sync && uv run pytest -q --cov=deaddit --cov-report=term-missing
```

- Python: 3.13 only (owner decision 15) — single CI entry.
- Coverage: report from day one, gate later — initial `--cov-fail-under=35` raised to 60 once the
  domain/ services are extracted (solo-project-sensible; gates on untested legacy glue would just
  incentivize deleting tests).
- Import-hygiene check as a tiny pytest plugin/assertion test: walk `domain/**` ASTs, fail if
  `from flask import request|session|current_app` or `import *` appears — encodes the graph rules
  mechanically.
- Add `[tool.pytest.ini_options]` and `[tool.coverage.*]` sections to `pyproject.toml`; add
  F403/F405/B008-relevant ruff tightening (`select += ["F403","F405","S"]` subset where cheap).

### Config/secrets hygiene

Split the current hybrid (`config.py:47-88`) into two honest halves:

| Kind | Mechanism | Examples |
|---|---|---|
| Static env config | `config.py` dataclass read once from env at startup; immutable per process | `DATABASE_URL`, `API_TOKEN`, `SECRET_KEY`, `PRODUCTION`, bind host/port |
| Runtime settings | DB `Setting` rows behind `settings/service.py` (admin-editable, cached with TTL) | `OPENAI_API_URL`, `MODELS`, `API_BASE_URL`, generation defaults, `DEFAULT_DATA_LOADED` |

- **Secrets become env-only**: `SECRET_KEY`, `API_TOKEN`, `OPENAI_KEY`(→ LLM Lead's provider
  config) come from environment/.env; DB rows holding these are deprecated. Migration: settings
  service prefers env, logs a warning when a stale DB copy exists, and a
  `flask secrets-drain` command exports DB-stored secrets to stdout for one-time transfer and
  deletes the rows. Kills the "anyone with the sqlite file has the API key" problem
  (`DEFAULTS` at `config.py:22-24`).
- Ship `.env.example` listing every variable with safe defaults; docker-compose gains an
  `env_file:` + documented `environment:` passthrough (today `API_TOKEN` silently never reaches
  the container — see Current State).
- `Config.get` per-request DB hits disappear: settings service caches with short TTL + explicit
  invalidation on admin save (`admin.py:1372 save-config` calls the setter, which busts cache).

### Repo hygiene & ops

- Purge untracked artifacts (implementer runs): `git clean -fdx` scope-reviewed, or explicitly
  `rm -rf __pycache__ .pytest_cache .ruff_cache app.log flask.log data.db building/ .claude/`
  and `deaddit/**/__pycache__`; keep `instance/deaddit.db` (live data) and refresh
  `deaddit.db.backup` via `sqlite3 instance/deaddit.db ".backup instance/deaddit.db.backup"`
  before Phase A3 migrations (WAL checkpoint included in `.backup`).
- `.gitignore` fixes: remove `gunicorn.conf.py` and `wsgi.py` lines (track those files); remove
  bare `lib/`; keep `*.log`, `instance/`, `*.db`; add `.venv/`, `dist/`, `refactor/` decision
  left to owner (plans likely shouldn't ship in the product repo).
- Logging: consolidate on stdlib `logging` (drop loguru — two systems today, `loader.py:11` vs
  `__init__.py:34`); configure one dict-based config with `RotatingFileHandler` (10MB × 5) for
  bare-metal runs and plain stdout for Docker; gunicorn already logs to stdout
  (`accesslog = "-"`).
- Backups: **none automated for now** (owner decision 14, 2026-08-24). Document the manual
  `sqlite3 instance/deaddit.db ".backup <dest>"` command in the README; revisit automation
  (cron/systemd timer, litestream) only if the owner asks. The pre-A3 one-time copy of the
  live DB remains mandatory as the migration rollback point. Compose volume remains source
  of truth.
- Dockerfile: multi-stage not warranted; fix base pin (currently `python:3.10` vs
  `requires-python >=3.9` vs actual 3.13 dev — standardize on 3.13-slim), install from
  `pyproject.toml`/lock (single source of truth), run as non-root, `CMD` gunicorn.
- Docs: README refresh outline — What it is (Agentic Reddit), architecture diagram (web/worker/db
  processes), quickstart (compose with `.env`), configuration reference (env vs settings table),
  operator guide (backup, restore, logs, migrations `flask db upgrade` on start), API summary,
  development guide (uv, pytest, ruff), pointer to `refactor/*.md` plans.

## Key Decisions & Tradeoffs

1. **Keep Flask + strangler, no framework rewrite.** Options: FastAPI rewrite, Django. Chosen
   Flask: 7,900 LOC with server-rendered Jinja + socketio + admin; a rewrite risks the one thing
   that matters (83MB of live content and a working product) for zero user-visible gain, and
   violates the orchestrator's boring-tech constraint. Cost: we keep Jinja/WTForms-less form
   handling debt in admin.py; mitigated by blueprint split, not rewrite.
2. **App factory over module singleton.** Cost: touches every `from deaddit import app` import
   site (`api.py:8`, `routes.py:5`, `websocket.py:11`). Benefit: testability (per-test app/db),
   kills import-order landmines (`__init__.py:62-128`). Non-negotiable prerequisite for every
   other lead's testing story.
3. **DB-as-queue + APScheduler standalone over Redis queues.** Tradeoff: we hand-roll claim
   leases (~50 lines) and lose battle-hardened retry semantics. Rationale: single box, no new
   service, `Job` table already half-does this (`jobs.py:83-133`), and the orchestrator brief
   forbids Kafka-class machinery. Escape hatch documented (Postgres/SQLite queue libs or arq if
   agentic runtime needs more).
4. **Delete gevent rather than adopt it.** Evidence above. Adopting gevent would mean
   monkey-patching + gunicorn gevent workers + verifying every lib (requests, apscheduler
   threads) cooperates — complexity purchased for nothing since nothing blocks the event loop
   today (sync workers, one thread per request).
5. **Keep `/api/ingest` as wrapper rather than delete.** External tooling/docs reference it
   (README documents the API; seed loader historically used it). Cost: one extra serialization
   hop retained; benefit: backward compatibility for anyone scripting content ingestion.
6. **Env-only secrets vs DB-stored.** Tradeoff: settings UI can no longer edit the OpenAI key
   (admin settings page, `admin.py:1327+` loses a field); operators edit `.env` + restart.
   Rationale: secrets in a world-readable DB file next to an 80MB backup is a real incident
   waiting; admin-editable *non-secret* knobs remain in the settings service.
7. **SQLite now, Postgres later, decided by measurement.** Avoids migrating twice and avoids a
   service the product doesn't need; the ORM+migrations discipline above is precisely what keeps
   the door open. Explicit tripwire: recurring lock timeouts under agentic load or >~2GB DB.

## Phased Roadmap

Sequencing contract with other leads (who lands first, and why):
**A0/A1/A2 are prerequisites for everyone** — nobody can test against the singleton, and LLM/UX
plans assume runnable CI. **A3 (migrations) must precede Platform Dynamics' Vote/karma tables**
and Agentic Core's memory schema — they extend my baseline, not `create_all()`. **A4 (service
layer) is the API Agentic Core consumes** instead of jobs/loader orchestration. **A5 (worker) is
where Agentic Core's runtime deploys.** UX Lead's admin/feed work rides on A1 (blueprints) and
benefits from A2's safety net; feed pagination SQL rewrite (in A3) must coordinate with their
ranking changes (Platform Dynamics ranking supersedes random order eventually — index choice
there is `(subdeaddit_name, created_at)` which serves score-ordered feeds too once a score
column exists).

### Phase A0 — Packaging truth & repo hygiene (S)

Scope: reconcile dependencies (move `apscheduler`, `click`, `flask-socketio` into
`[project.dependencies]`; delete `gevent`; delete `requirements.txt` or make it
`-e .`); regenerate `uv.lock`; un-ignore + commit `gunicorn.conf.py`, `wsgi.py`; purge junk files
listed in Repo hygiene; add `.env.example`; standardize Python 3.13 pins; consolidate logging
config.
Acceptance: fresh clone → `uv sync && uv run python -c "import deaddit"` succeeds (fails today:
lock lacks apscheduler/flask-socketio); `git ls-files` includes gunicorn/wsgi configs; no
`__pycache__`/logs/`data.db` on a clean checkout; `uv run python app.py` still boots the app.

### Phase A1 — App factory & import discipline (M)

Scope: introduce `extensions.py`; convert `__init__.py` to `create_app(config=None)`; wrap
`api.py`, `routes.py` views into Blueprints registered in `create_app`; delete star imports;
move `db.create_all()`/`initialize_defaults()`/`restart_pending_jobs()` out of import time
(create_all temporarily replaced by a `flask init-db` command until A3); move
`authenticate()` to blueprint-level hooks; keep module-level shims
(`deaddit/__init__.py` exposing lazy `app` for wsgi) during transition, removed in A6.
Acceptance: importing `deaddit.models` (or any submodule) performs **zero** I/O (verified by test
that imports modules in a subprocess and asserts no sqlite file is created); app boots via
`gunicorn deaddit.wsgi:app` and dev `app.py`; manual smoke: home page, one subdeaddit page, admin
login, one generation job end-to-end against a stub LLM URL.

### Phase A2 — Test foundation & fake LLM seam (M)

Scope: pytest config in pyproject; conftest fixtures (`app`, `client`, tmp/in-memory DB);
smoke tests for public routes; `deaddit/llm/client.py` already exists from LLM Phase 1
(Wave 0) — A2 writes `tests/fakes.py` FakeProvider against its interface (legacy regex
parsers stay frozen in `loader.py`/`jobs.py` until the Wave 6 deletions; the client never
grows a salvage path — roadmap Resolution 11); CI workflow (ruff + pytest, Python 3.13);
AST import-rule test for `domain/` (empty then, enforced as packages appear).
Acceptance: `uv run pytest` green in CI on a clean clone with **no network egress** (fake LLM
injected); coverage reported; `uv run ruff check .` clean including new F403/F405 rules; smoke
test covers setup-page and populated-page paths.

### Phase A3 — Migrations, pragmas, indexes, feed SQL (M)

Scope: Flask-Migrate; baseline migration autogenerated from models; `stamp head` procedure
documented and executed against live DB (after fresh backup); `db.create_all()` deleted;
pragma event listener; four composite-index migrations; rewrite `routes.index` +
`utils.paginate_posts_with_model_cycling` to SQL LIMIT/OFFSET with cached model list.
**Gate for Platform Dynamics (votes schema) and Agentic Core (memory tables):** they branch
after this merges.
Acceptance: `flask db upgrade` on a copy of the live 83MB DB completes <30s with zero row loss
(row counts pre/post equal); feed page renders identical content classes (spot-check vs old
algorithm output for same seed); `EXPLAIN QUERY PLAN` for `/d/<name>` shows index usage, no full
scan; app runs with WAL mode (`PRAGMA journal_mode` returns `wal`).

### Phase A4 — Service layer, kill self-HTTP ingest (M)

Scope: `services/content.py` (extraction of `api.py:30-179,328-373` bodies; canonical per
Resolution 1); migrate callers: loader `create_*`, `jobs._execute_create_*` (replace `requests.post` to
self with direct service calls), CLI, `data/load_seed_data.py`; `/api/ingest` becomes a
transitional wrapper (deleted at Wave 6, owner decision 8);
delete `loader.ingest()`, `loader.get_api_base_url/get_api_headers`, `jobs.get_api_base_url/
get_api_headers`; fix `loader.py:621` key bug (obsoleted by deletion).
Acceptance: `grep -rn "requests.post" deaddit/` shows zero calls to own base URL (network mocks
in tests assert none attempted); unit tests for content-service validation + transactions; a
generated post via admin appears with correct IDs/threading; external POST to `/api/ingest`
returns identical response shape as before (contract test recorded pre-change).

### Phase A5 — Dedicated worker process (L)

Scope: `runtime/` package; `deaddit-worker` entrypoint; Job claim/heartbeat columns + atomic
claim; crash-recovery sweep generalizing `restart_pending_jobs`; compose gains `worker` service;
web process no longer starts any scheduler; websocket progress path switched to poll/push from
web process reading Job rows (coordinate final mechanism with UX Lead's admin dashboard work).
This phase is the deployment target Agentic Core builds its runtime on.
Acceptance: with 2 gunicorn web workers + 1 worker process, creating N jobs executes each
exactly once (claim-race test + live observation in admin jobs list); `kill -TERM` worker
mid-job → job resumes (not duplicates) after restart; web-only restart does not interrupt a
running generation; `docker compose up` brings up web+worker healthy.

### Phase A6 — Config/secrets split, cutover cleanup, docs (M)

Scope: settings service with TTL cache replacing `Config.get` call sites; env-only secrets +
drain command; admin settings page updated (secret fields removed — coordinate with UX Lead);
remove transition shims/lazy-app compat from A1; README refresh per outline; backup docs
reduced to the manual `.backup` command (owner decision 14 — no automation for now);
Dockerfile final form (gunicorn CMD, non-root, install-from-lock).
Acceptance: no `OPENAI_KEY`/`SECRET_KEY`/`API_TOKEN` rows remain in a migrated DB (query
returns empty); settings edits reflect within TTL without restart; fresh-machine deploy from
README alone (compose + `.env`) reaches a generating system; `grep -rn "from deaddit import app"
deaddit/` returns nothing outside wsgi entrypoint.

### Explicit DELETION ledger (what dies, when)

| Target | Origin | Deleted in |
|---|---|---|
| `loader.ingest()` + self-HTTP calls | `loader.py:1031,1526,1596,2831` | A4 |
| Self-HTTP `requests.post` in job executors | `jobs.py:1287ff` | A4 |
| Duplicate LLM client in jobs (`_send_openai_request`, `_parse_json_response`) | `jobs.py:598,688` | transport merged into `llm/client.py` (LLM P1/A2); parsers frozen → deleted Wave 6 (AgenticCore D1/D3, Resolution 11) |
| `db.create_all()` at import | `__init__.py:82` | A3 |
| Star imports `from .api/routes import *` | `__init__.py:127-128` | A1 |
| Global `before_request authenticate()` path matching | `__init__.py:44-59` | A1 (blueprint hooks) |
| `gevent` dependency | `pyproject.toml:19` | A0 |
| `requirements.txt` | repo root | A0 (superseded by lock) |
| Distribution-test harnesses `test_*_distribution` | `loader.py:426,522` (+CLI twins) | A4 (recreated as real unit tests in A2 where valuable) |
| `loader.py`, `jobs.py`, `api.py`, `routes.py` monolith shells | whole files | A6 (fully absorbed into domain/runtime/web/api) |
| `data/load_seed_data.py` self-HTTP seeding | `load_seed_data.py:8-9` | A4 (rewritten on the content service) |
| Stale artifacts: root/deaddit `__pycache__`, logs, `data.db`, caches, `building/`, `.claude/` | disk | A0 |
| `Config` DB/env hybrid class | `config.py` | A6 (split into config + settings service) |
| `/api/ingest` public wrapper | `api.py:27-179` (→ `api/v1.py`) | Wave 6 (AgenticCore D2; owner decision 8) |

## Risks & Mitigations

1. **Blueprint conversion breaks subtle routing/auth behavior** (e.g. `authenticate()`'s
   path-prefix rules, error handlers returning JSON for pages). Mitigation: A1 ships route-table
   dump comparison (`app.url_map` before/after asserted equal in a test) + manual smoke checklist;
   auth hook converted last within A1.
2. **Migration baseline mismatches drifted live schema** (`create_all`-only history means the
   83MB DB may differ from current models). Mitigation: autogenerate diff reviewed by hand;
   baseline applied to a copied DB first; fresh backup mandatory (Repo hygiene step precedes A3);
   rollback = restore backup + previous code tag.
3. **Double job execution during web↔worker transition** (old in-web scheduler + new worker).
   Mitigation: A5 flips atomically in one release — compose starts worker, web code no longer
   calls `start_scheduler()` in the same commit; claim protocol makes races harmless even if a
   stale process lingers; acceptance test covers the race.
4. **Long-running LLM jobs killed by worker deploys**. Mitigation: heartbeat/resume sweep;
   graceful_timeout 30s in gunicorn config analog for worker (`scheduler.shutdown(wait=True)`);
   jobs already persist partial results (`_get_partial_*_result`, `jobs.py:227-255`).
5. **Other leads build against moving foundations** (e.g. Platform Dynamics starts voting schema
   before A3 lands). Mitigation: sequencing contract above is explicit; orchestrator's master
   roadmap should gate PD/Agentic-Core schema work on A3 merge and Agentic-Core runtime on A5.
6. **Coverage gates encourage low-value tests on legacy glue.** Mitigation: gates apply to
   `domain/`, `settings/`, `runtime/` paths first (`--cov` scoping), legacy shells exempt until
   deleted in A6.
7. **Secret rotation gaps** — DB-stored keys may exist in backups/pre-refactor clones.
   Mitigation: drain command prints + deletes rows; recommend owner rotates OPENAI_KEY/API_TOKEN
   once at A6 regardless.

## Open Questions — resolved 2026-08-24 (owner decisions) unless noted

1. Backup policy → **none for now** (decision 14): no off-box replication, no cron
   automation; manual `.backup` documented only. A6 ops scope shrinks accordingly.
2. `refactor/` plans → **committed to the repo, never pushed to any remote** (decision 16);
   `building/` notes stay untracked (historical scratch).
3. Python floor → **3.13 only** (decision 15): single CI entry, `requires-python >=3.13`,
   Docker `python:3.13-slim`.
4. Socket.io fate → still lead-level: UX Phase 6 decides push vs polling; roadmap
   Resolution 5 gates any transport change on LLM Phase 4 verification.
5. Domain rename → confirmed out of architecture scope (unchanged).
