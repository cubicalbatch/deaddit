# Deaddit Refactor — Orchestrator Progress Ledger

Append-only state file maintained by the orchestrator per `AGENT_START.md`.
Binding order: `AGENT_START.md` → `00-master-roadmap.md` → lead plans.
Run started: 2026-08-24 · Branch: `refactor` · Commits local only, NEVER push (decision 16).

## Key facts for downstream leads

- **Live endpoint**: `http://100.84.49.52:8080/v1`, model string **VERIFIED** `qwen3.8-27b`
  (exact match vs GET /models, 2026-08-24, LLM-1 independent tester) · tools probe PASS same
  day (finish_reason=tool_calls, schema-valid args) — Resolution 11 agent-phase gate GREEN;
  re-probe at cohort creation per decision 2. Deterministic CI uses the fake provider only.
- **Canonical content service**: `deaddit/services/content.py` — created in A4; later leads
  EXTEND it. A second persistence path is the failure to reject on sight (Resolution 1).
- **Tool-calls-only** everywhere (Resolution 11 / decision 17). No new code parses
  unstructured JSON from model output. `loader.py`/`jobs.py` legacy parsers frozen until
  Wave 6 deletions.
- **Pre-A3 DB copy (mandatory, decision 14)**: timestamped copy of `instance/deaddit.db`
  (~83 MB production data) taken BEFORE any migration runs. Status: NOT YET TAKEN.
- pydantic v2 adopted (decision 18) · Python 3.13 only (decision 15) · gevent deleted in A0
  (Resolution 5) · Flask stays · SQLite stays · no new services/brokers.
- Commit convention: `refactor(<phase-id>): <summary>` · app runnable at every commit.
- **Repo state at run start**: branch `refactor`; five files carried pre-existing uncommitted
  owner modifications (+77/-1): `deaddit/__init__.py`, `deaddit/admin.py`, `deaddit/api.py`,
  `deaddit/config.py`, `deaddit/utils.py`. Audit result: see Rulings & incidents.
  Leads: never `git add -A`/`git add .`; stage explicit paths only; if your slice must
  commit one of these files, note the ridden-along hunks in the commit message and report.

## Phase ledger

| phase | lead | status | commit | verdicts | notes-for-downstream |
|---|---|---|---|---|---|
| A0 packaging truth & hygiene | LeadA0 | **done** | be4f9c3 baseline · 633c953 docs · bc2f4d4 main | 10/10 PASS (indep. tester; fresh-clone uv sync, gunicorn/wsgi tracked, gevent zero-ref, compose env passthrough live-verified in container, gunicorn serving w/o debug) | `deaddit/logging_config.py configure_logging()` is THE logging entrypoint (stdlib; DEADDIT_LOG_LEVEL/DEADDIT_LOG_FILE); loguru banned repo-wide. `wsgi.py`+`gunicorn.conf.py` tracked; compose service renamed `web`, builds locally, env_file .env + API_TOKEN/SECRET_KEY/OPENAI_KEY passthrough; `.env.example` canonical list. pyproject sole dep source, uv.lock frozen, py>=3.13. Owner PRODUCTION lockdown landed as be4f9c3. Known: gunicorn preload_app=True fork hazard until A5 moves scheduler; ~158 legacy ruff hits left for touching phases; host port 5000 was occupied during tests — testers bind ephemeral ports; year-old deaddit.db.backup must NOT be trusted — A3 takes a FRESH pre-migration copy. |
| UX-0 quick wins | LeadUX0 | **done** | 498255d | 4/4 PASS (indep. tester, axe 4.10.2 live :5097; collapse keyboard PASS after fix-loop on toggleComment) | Scoped `.comment-collapse-bar` <style> in post.html ~L70-99 → fold into tokens.css at UX-1. `.empty-state` component in partials/post_list.html → generalize at UX-2. Dead `GenerationTemplate.query.all()` in generate() route → drop at UX-5/P4. Pre-existing axe leftovers (contrast serious x21/x101, heading-order) = UX-1/UX-2 fodder. |
| LLM-1 client consolidation | LeadLLM1 | **done** | deeb753 | 4/4 PASS (indep. tester; C2 full admin-job flow vs stub LLM on throwaway DB copy; C3 retry/typed-error after fix-loop) | `deaddit/llm/{client,transport,errors}.py` exist: ChatRequest/Sampling/ChatResult, complete(), STOP_VALUES, Transient/PermanentLLMError — LLM-2 EXTENDS this package. X-Request-Id: base id logged, wire header carries `<base>-<attempt>`. Mechanical ruff cleanup ridden along in loader.py/jobs.py (no behavior change). KNOWN BUG for A4: loader.get_api_base_url() reads Config key 'get_api_base_url()' instead of 'API_BASE_URL' (jobs.py correct). Legacy parsers frozen per Res. 11. |
| A1 app factory & blueprints | — | pending | — | — | Wave 1; strictly before A2; kills import-time side effects; route-map equality test |
| A2 tests + fake-LLM seam + CI | — | pending | — | — | Wave 1; pytest fixtures, smoke tests, GitHub Actions |
| A3 migrations/WAL/indexes/feed-SQL | — | pending | — | — | Wave 2 GATE for all new schema + UX pagination; DB copy must exist before first migration |
| LLM-2 capability probing + tool-arg validation | — | pending | — | — | Wave 2; EndpointCapability verdicts; tools probe PASS required before agent-phase live tests count |
| UX-1 tokens + asset hygiene | — | pending | — | — | Wave 2; tokens.css, self-hosted assets, real dark palette, jQuery/Select2 removal |
| A4 service layer; self-HTTP ingest deleted | — | pending | — | — | Wave 3; creates `deaddit/services/content.py` (Resolution 1); `/api/ingest` → wrapper |
| AgenticCore P0+1 | — | pending | — | — | Wave 3; restore `deaddit/agents/` fresh; Agent/Run/Turn/ToolCall/Memory schema; `deaddit agent run-once`; feature-flagged off by default |
| UX-2 feed reading experience | — | pending | — | — | Wave 3; PostCard/SortBar/PageNav |
| UX-3 CommentTree rebuild | — | pending | — | — | Wave 3; depth cap, accessible collapse, permalinks |
| LLM-3 accounting + routing | — | pending | — | — | Wave 3; LLMUsage ledger; ModelRoute replaces substring matching + stale MODELS global |
| A5 dedicated worker process | — | pending | — | — | Wave 4; claim/heartbeat, crash sweep, compose worker; web stops scheduling |
| AgenticCore P2 scheduler + admin visibility | — | pending | — | — | Wave 4; next_run_at wake, boot recovery, budgets, memory summarizer, admin API+pages; UI-driven lifecycle, nothing runs by default (decision 1) |
| D1 votes/karma/backfill | — | pending | — | — | Wave 4; Vote table, cast_vote, exact-sum backfill of history; DB copy mandatory before DDL |
| D2 ranked feeds | — | pending | — | — | Wave 4, after D1; hot/top/new/rising ORDER BY replaces interim SQL LIMIT/OFFSET sort |
| D3 notifications/inbox | — | pending | — | — | Wave 4, after D1 (parallel with D2); get_inbox/mark_inbox_read contract |
| UX-4 profiles/people/setup/search | — | pending | — | — | Wave 4; incl. cheap SQLite site search (decision 10) |
| AC P3 parity cohort | — | pending | — | — | Wave 5 LONG POLE — start first; window target ≥24 h, floor 6 h + expanded sampling (decision 19); criteria per agentic-core.md; (d) = reviewer samples ≥20 items, flagged for owner post-hoc review |
| D4 moderation MVP | — | pending | — | — | Wave 5 parallel lane |
| D5 history seeding | — | pending | — | — | Wave 5 parallel lane |
| LLM-4 streaming | — | pending | — | — | Wave 5; sync gunicorn workers + SocketIO(async_mode="threading") per Resolution 5; live token streaming verdict required |
| LLM-5 prompt versioning | — | pending | — | — | Wave 5 parallel lane |
| UX-5 admin modernization | — | pending | — | — | Wave 5; DenseTable, settings IA + empty-means-unchanged secrets, streamed job logs; bullet-mask bug dies here at latest |
| AgenticCore P4 deletions | — | pending | — | — | Wave 6; GATED by parity verdict; legacy executors/parsers/loader orchestration die; CLI swap; deletion greps must return zero |
| A6 config/secrets split + docs | — | pending | — | — | Wave 6; env-only secrets + drain command; README refresh; manual sqlite3 .backup documented (decision 14) |
| UX-6 live updates + ThoughtLog | — | pending | — | — | Wave 6; click-to-load ticker on `/live` (decision 11); live verdict required |
| D6 anti-degeneracy instrumentation | — | pending | — | — | Wave 6; detectors, demotions, PlatformDaily rollups, cost-per-engagement dashboard |

## Rulings & incidents

- 2026-08-24 — Run start. Five dirty source files found (owner WIP, +77/-1, see Key facts).
  Orchestrator commissioned read-only audit before Wave 0 spawn; result recorded below when
  available.

- 2026-08-24 — DirtyAudit verdict (scout): the five pre-existing dirty files are ONE
  coherent complete feature — `PRODUCTION` mode lockdown: `config.py` adds default
  `PRODUCTION=false`; `utils.py` adds `@production_disabled` (abort(404) when enabled);
  `__init__.py` injects the boolean into template context; `admin.py` (~35 routes) and
  `api.py` (`/api/ingest`, `/api/ingest/user`) apply it outermost. No truncated hunks; no
  contact with frozen `jobs.py`/`loader.py` parsing semantics. Ruling: KEEP-as-baseline;
  A0 lead lands it as its FIRST commit (`refactor(A0): baseline — owner PRODUCTION-mode
  lockdown WIP`) staging exactly those five paths, so no later phase commit carries
  foreign hunks. Downstream: preserve `@production_disabled` decorators when relocating
  routes (A4 `/api/ingest` wrapper, A1 blueprint move); decorator reads DB-backed Config
  per request — acceptable until A6 settings-service TTL cache replaces the read path.

- 2026-08-24 — Wave 0 closed. UX-0 (498255d) + LLM-1 (deeb753) PASS under independent
  testers; both reports show genuine fix-loop traces; accepted. Rulings on escalations:
  (1) DB `API_TOKEN=''` Setting row overriding env → assigned to A6 config/secrets split;
  A1/A4 leads: when touching Config.get, do NOT silently change precedence — note it.
  (2) SQLite path has no env override → architecture lead folds DEADDIT_DB_PATH-style env
  into the A-phase config work (A6 settings service); interim: testers copy instance/.
  (3) axe leftovers tracked as UX-1/UX-2 fodder. LLM-1's loader.get_api_base_url()
  wrong-key bug → A4 must fix during ingest-wrapper work.
