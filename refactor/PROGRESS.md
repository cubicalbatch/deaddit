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
  (~83 MB production data) taken BEFORE any migration runs. Status: **TAKEN 2026-08-24
  17:44** — `instance/deaddit.db.pre-a3-20260824T174408`, md5 cb4c9528…f27 identical to
  live DB at copy time. Old `deaddit.db.backup` (2026-07) untrusted (A0 finding).
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
| A1 app factory & blueprints | LeadA1 | **done** | 8c12505 | 5/5 PASS (indep. tester; import-zero-IO sweep, gunicorn+dev boot, stub-LLM e2e job, route-map equality 60==60 vs baseline JSON, init-db 9 tables) | `from deaddit import create_app`; extensions at `deaddit.extensions` (db/cache/socketio init_app); blueprints `deaddit.routes.bp`('web') / `deaddit.api.bp`('api'); endpoints prefixed web.*, api./admin.* unchanged. create_app(config=None) takes dict/obj overrides → A2 conftest uses sqlite:// or tmp-path. wsgi.py moved into package (deaddit.wsgi:app), Dockerfile CMD matches. Scheduler still starts in web process by design until A5. Route-map baseline: tests/a1_route_map_baseline.json; render-smoke test pattern reusable. |
| A2 tests + fake-LLM seam + CI | LeadA2 | **done** | 5854af0 | accepted (see Rulings 2026-08-24; no separate tester tally recorded) | Fake-LLM seam = `deaddit/llm/provider.py` (production falls back to transport.post_chat); ALL deterministic LLM tests go through `tests.fakes.FakeProvider`, never network. `ruff format --check` NOT in CI (9 files unformatted) — first lead with appetite may run a format pass as rider; A6 hygiene fallback. |
| A3 migrations/WAL/indexes/feed-SQL | LeadA3 | **done** | b4b8d46 | 9/9 PASS (indep. TesterA3c; upgrade-on-copy <30s zero row loss all 9 tables, EQP index-usage no SCAN, PRAGMA wal via app engine, route-map equality, pytest 16p/1s + ruff clean, live boot :5097 read-only vs prod DB, deletion greps zero, stamp procedure documented) | Alembic baseline 359878740bb0 → head 5b2dab0b6816; ALL new schema now branches from migrations/ (Res 6 open). Driver pattern for revisions: create_app({'SQLALCHEMY_DATABASE_URI': explicit}) + test_cli_runner — NEVER `flask --app deaddit.wsgi db …` (boots LIVE instance DB). Fresh DB = flask init-db; create_app no longer creates tables. Live DB stamped+upgraded post-PASS: alembic_version=5b2dab0b6816, WAL, 4 composite indexes, row counts == pre-a3 copy. Rollback point intact. Feeds deterministic created_at DESC,id DESC; ?page=N+has_more unchanged; ?models= still threaded (dies at decision 12 later). Inherited WIP folded incl. subdeaddit() NameError fix + regression test. |
| UX-1 tokens + asset hygiene | LeadUX1 | **done** | 0178f59 | 10/10 PASS (indep. tester; C9 PASS* with attribution — see Rulings; cold-load zero third-party origins, scripted WCAG AA both themes both surfaces, golden renders 12 files, render-smoke all page classes live :5090) | tokens.css = primitives+semantic roles ONLY (component rules live in style.css; legacy aliases documented until UX-2 rename). Assets self-hosted under static/vendor/. Dark palette designed ramp (#0f1012/#16181b/#1d2024). Theme toggle real button, pre-paint init kept. jQuery/Select2 GONE from public site; admin keeps self-hosted copies until UX-5 (settings.html select2). htmx NOT vendored yet — UX-2 vendors at first use. Bootstrap compat shim (~30 token-styled lines in style.css) until UX-2/UX-4 rebuild PageNav/buttons; setup.html page-scoped v4 until UX-4. |
| LLM-2 capability probing + tool-arg validation | LeadLLM2 | **done** | eae7bb4 (+143150e baseline refresh) | 4/4 PASS (indep. TesterLLM2; LIVE probe verdict: /models sole entry qwen3.8-27b reconfirmed, finish_reason=tool_calls, args {"message":"ping"} schema-valid, supports_tools=1 persisted; fix-loop round on LAST_PROBE_EVIDENCE binding, re-green 32/32 deterministic) | `deaddit.llm.capabilities`: ensure_tools_allowed(api_url, model, auto_probe=...) + typed CapabilityError(attrs api_url/model/request_id) — AgenticCore calls with auto_probe=True at agent/cohort creation (decision 2 re-probe). endpoint_capability table via migration 51667ad06eae (pre-llm2 snapshot taken). validate_tool_args/build_tool_results: schema-invalid or unknown tool args → JSON error tool-result fed back to model; invalid-args-in-valid-envelope ⇒ supports_tools=False VERDICT (Res 11 conservative). Client gating uses auto_probe=False default (deterministic under FakeProvider). supports_streaming column NULL until LLM-4. Admin pages GET/POST /admin/capabilities[...] exist; manual override wins over probes. LLM-3: accounting hooks wrap provider call inside client.complete(). |
| A4 service layer; self-HTTP ingest deleted | LeadA4 | **done** | b1ab61b1 | 8/8 PASS (indep. tester incl. deny-all no-socket runtime harness, 9 static+behavioral no_self_http tests, 20 service unit tests, 28 ingest-contract tests byte-identical shapes/messages; Resolution-1 sweep caught admin.load_default_data_api direct-write in fix-loop round 1 → PASS; 105p/1s + ruff clean) | `deaddit/services/content.py` = SOLE persistence path (Res 1): keyword-only create_post/create_comment/create_user/create_subdeaddit, transactional self-committing, need app ctx, return ORM objects; user/subdeaddit args are STRINGS; ContentValidationError(ValueError); created_at honored only where column exists (Post/Comment) — User/Subdeaddit LACK created_at columns (D5 needs additive migration first). get_available_models moved into content.py (re-exported by api.py). loader.get_api_base_url() wrong-key bug obsoleted (helpers deleted). Cache invalidation hook runs after every mutation. |
| AgenticCore P0+1 | LeadAC01 | **done** | c87c193 | 10/10 PASS (indep. ACTester1; LIVE run-once trace: 7 turns/13 audited actions/27.4k tokens, comment 36102 via service stamped agent:qa_ac1_agent rendered on :5091; auto-probe proven at agent create; 193p/0f after 2 fix rounds) | Tables agent/agent_run/agent_turn/tool_call/agent_memory via migration c8f2d…(c8f2a4e61b9d, chains on LLM-3's a3f1c9d2b4e7; head single throughout). Flag Setting AGENT_RUNTIME_ENABLED='false' gates FUTURE background runs (P2 gates boot-scan/wakes on it); explicit run-once always allowed. CLI deaddit agent create|list|run-once (pyproject scripts entry). Guardrails: rate caps count SUCCESSFUL calls; correction cap = 2 consecutive kind=rejected force-finishes; ToolCall.turn_id nullable; ToolCall.result stored truncated-but-valid JSON. P2 hooks: run_once() marks stale running runs interrupted (>max_run_seconds+60s), refuses concurrent, draws next_run_at only when enabled; agent.config budget keys api_url/model/min_delay/max_delay/max_actions_per_run=12/max_run_seconds=300; consecutive_failures>=5 → disabled. Vote tool stub ok=False until D1 cast_vote flips registration in tools_write.py. |
| UX-2 feed reading experience | LeadUX23 | **done** | 74be688 | PASS (indep. TesterUX2; FAIL r1 → fix+rider → PASS r2; axe both themes zero, WCAG matrix scripted, cold-load zero third-party) | PostCard/SortBar/PageNav + htmx 2.0.4 vendored (load-more). Sort whitelist new\|top between FIXED ORDER BY exprs only (main-approved extension; top = upvote_count DESC interim per Res 3 staging; Res 4 rename untouched). New token --accent-strong(+hover) because solid --accent can't carry AA body text (ramp-700 #bf3000 = 5.77:1 w/ white). Legacy alias block RETIRED from tokens.css; bootstrap-compat layer deleted from public site. Macros in partials/_macros.html: avatar/pagenav/vote_widget/chip/empty_state + post_card/sort_bar partials; pagenav preserves query args. |
| UX-3 CommentTree rebuild | LeadUX23 | **done** | 120549a | PASS (TesterUX3 partial-evidence rounds + fresh finisher TesterUX3c; 2 fix loops; axe post+profile zero violations both themes; 1000-comment TTI ~64ms server / ~350ms browser) | Depth cap: native nesting stops at 8, flattened tail with real-depth badges + 'Continue this thread' jump. Deep-link force-expands ancestor chain + highlight flash (~2s). sessionStorage key deaddit.collapsed is PRUNE-ONLY semantics — never snapshot-on-visit. format_content_html() in utils.py is THE untrusted renderer; post.html has exactly TWO \|safe inputs (post_body_html, node.content_html), both formatter outputs — static test enforces. user_profile() passes page/has_more for shared partial. |
| LLM-3 accounting + routing | LeadLLM3 | **done** | 5683be1 | PASS (indep. TesterLLM3; 9 criteria incl. per-attempt failure rows, precedence chain override>tier>default>ApiEndpointConfig>OPENAI_MODEL, $0-exact local vs NULL unpriced; M1–M5 migrations/globals; 191p→fix-loop→green; scoped admin dashboard = UX-5) | Tables llm_usage/model_price/model_route via a3f1c9d2b4e7. Ledger: exactly one LLMUsage row per attempt incl failures (error_type on transient); CapabilityError pre-flight = zero rows; estimated_cost NULL when unpriced (never fake $0). Routing replaces substring matching + MODELS global/select_model retired (zero refs). Read-only JSON APIs GET /admin/api/usage/summary + /admin/api/routes added (baseline refreshed additively +2, justified — dashboards/widgets stay UX-5). D6 join keys: day=date(created_at), dims action/agent/api_url/model; SUM null-safe. |
| A5 dedicated worker process | LeadA5 | **done** | a3f3a96 | 8/8 PASS (indep. tester; exactly-once under 3-way claim race on LIVE qwen jobs; SIGTERM drain + kill -9 heartbeat-stale sweep recovery; web-restart independence; /proc-level zero-scheduler proof web+compose; compose up web+worker healthy; 224p/1s + ruff clean; prod-DB md5-verified restore protocol) | Migration head NOW b7e4c9a02f15 (job claim/heartbeat cols). Reusable contracts in deaddit/runtime/: claim.claim_job/heartbeat/sweep_stale_jobs(HEARTBEAT_STALE_MINUTES=5), liveness helpers (WORKER_HEARTBEAT_AT ≤90s; file worker-heartbeat), runner.JobRunner(env knobs poll/heartbeat/LANE_SIZE, priority lanes ≥8/≤3), nightly.NIGHTLY_JOBS + register_nightly_jobs(scheduler) = THE Res-10 home. Entrypoint console script deaddit-worker (runtime.scheduler:main). jobs.execute_job(job_id, app=<explicit>) — never lazy shim in threads. 'scheduler_running' Setting semantics = worker liveness ≤90s. Crash sweep is heartbeat-age-based (hard-killed orphan requeues only after staleness). THREE latent deploy bugs fixed: Dockerfile phantom root wsgi.py, init_db outside app ctx, migrations/ missing from image. Pre-A5 snapshot pre-a5-20260825T092952 + md5 ledger; restore verified. |
| AgenticCore P2 scheduler + admin visibility | LeadACP2 | **in_progress** | — | — | Consumes AC01 run_once hooks + A5 claim/liveness/nightly patterns; ≥10 autonomous runs/24h incl restart = live verdict; UI lifecycle per decision 1 |
| D1 votes/karma/backfill | LeadD1 | **done** | fb9441f (+7f9c04b infeasible export) | PASS (indep. tester; C01–C11 incl. byte-frozen rejection vocab, exact-sum reconciliation 0 violations posts+comments on copy AND live prod leg, idempotency after S==0→n≥2 fix, nightly fires in worker, guard test proving unflagged-prod refusal; 51 scoped green; 296p w/ 3 foreign failures at gate time) | Vote table via d1f0a93b7c25 (single head; seeds allow_downvotes='true'). cast_vote rejection vocabulary BYTE-FROZEN — agents surface reason verbatim, never reword. Prod backfill: 787,786 source='backfill' rows, live SUM==upvote_count & COUNT==vote_count everywhere; 48 capacity-infeasible items (list: refactor/d1-unbackfilled-infeasible.json) carry fabricated upvote_count w/ zero votes — expected for D2 top-sort. Karma basis = effective score CASE vote_count>0 THEN score ELSE upvote_count (legacy items protected until Wave 6). Nightly dynamics-recompute 03:30 via runtime.nightly. Agent vote tool LIVE (stub gone). TWO prod-write incidents remediated (restore + surgical cleanup, md5-ledgered snapshots both times) — permanent control: prod backfill ONLY via lead-invoked gated CLI (--i-know-this-is-prod + allow_production RuntimeError guard); implementers get throwaway URIs only. |
| UX-4 profiles/people/setup/search | LeadUX4 | **done** | 32866f8 | PASS (indep. tester; axe ×14 scans light+dark zero critical, WCAG matrix incl. placeholder fix 4.46→6.12:1, goldens regenerated from HEAD per Wave-3 ruling, render-smoke 11/11 classes live, injection probes escaped, NULL-bio regression locked by tests, keyboard nav) | /search single page, three sections (Communities ≤8, People ≤8, Posts paginated); decision 10 'cheap' honored: LIKE contains(autoescape=True), NO schema change (handshake with D1 closed no-overlap). PersonaPanel exposes traits/writing_style/interests NULL-safely. Profile tabs ?tab=posts|comments&page=N dedup-free. Setup wizard reuses admin.save_config_api + load_default_data_api (production_disabled inherited). Baseline refresh +GET /search re-applied atop AC-P2's e56ff10 lane-scoped refresh. Tester prod-write slip disclosed (transient Config.initialize_defaults) — third incident of the class, see Rulings. |
| D2 ranked feeds | LeadD2c | **done** | 3deb078 | PASS 9/9 (indep. TesterD2b r2; r1 TesterD2 caught hot-fragment SQLite integer-division bug (/45000→/45000.0), SortBar default-active mismatch, 3 stale test_ux3_comments assertions → fix-loop → re-green; full gate 422p/1s + ruff clean) | Migration head NOW d2c4f8a16e90 (on d1f0a93b7c25; ix_post_score + expression ix_post_hot_expr byte-interpolating HOT_SQL_FRAGMENT — INDEXES ONLY, no tables). `deaddit/dynamics/ranking.py` = THE §3 formula module (hot log-gravity real-division, rising 24h score/pow(h+2,1.8), Wilson z=1.96, up=(vc+score)//2); routes consume post_order_by/rising_filter; comment sorts top/new/best/controversial via single python key. EQP house convention reaffirmed: 'SCAN … USING INDEX' compliant, bare SCAN violation. PROD DB NOT YET UPGRADED (AC-P2 burn-in writes until ~11:07 UTC Aug 26; index leg deferred — apply d2c4f8a16e90 after burn-in per md5-snapshot protocol). Deviations: exploration slots→D6; top time-windows omitted (spec optional); Res-4 rename declined+re-slotted (see rulings). Rising on live data legitimately empty (newest post 2025-09-30 >24h). |
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

- 2026-08-24 — A1 closed (8c12505). Downstream facts: instance_path resolves to <repo>/instance
  regardless of CWD (A3 stamp procedure + A6 DEADDIT_DB_PATH env work). Host port 5000 is
  occupied by an unrelated service — ALL live-test agents bind ephemeral ports (UX-0 used
  :5097, A1 :5001/:5003; standing rule). Pre-existing, not A1 regressions: users_list.html
  crashes on NULL user.bio (`user.bio[:120]`; 0 NULL bios today) → UX-4; dead url refs
  admin.agents_dashboard/agent_detail in templates → AgenticCore P2. A1 fix-loop lesson for
  testers: route-map equality does NOT catch template-level url_for breakage — render-smoke
  every page class after blueprint/route moves.

- 2026-08-24 — A2 closed (5854af0). Wave 1 complete. Downstream: fake-LLM seam is
  deaddit/llm/provider.py (production falls back to transport.post_chat); every future
  deterministic LLM test goes through tests.fakes.FakeProvider, never network.
  `ruff format --check` NOT in CI (9 files unformatted) — first lead with appetite may run
  a format pass as rider; A6 hygiene fallback.

- 2026-08-24 — DirtyAudit2 verdict (scout): four NEW dirty files (extensions.py, routes.py,
  pyproject.toml, uv.lock) are INCOMPLETE A3 groundwork, coherent theme: sqlite WAL/FK
  pragma connect-listener + `Migrate()` singleton (NOT yet init_app'd) in extensions.py;
  routes.index() rewritten to SQL count + created_at LIMIT/OFFSET (matches architecture.md);
  additive flask-migrate 4.1.0 / alembic 1.19.1 / mako 1.4.1 in lock. BROKEN as-is:
  routes.subdeaddit() still calls removed `paginate_posts_with_model_cycling` → NameError
  on /d/<name>; duplicated `query = Post.query` line. loader.py/jobs.py untouched.
  Ruling: fold into A3 — LeadA3 completes the WIP (fix subdeaddit(), wire migrate init_app,
  dedupe) as the phase's first slice and stages those exact paths explicitly, noting
  ridden-along hunks in the commit message. Never `git add -A`.

- 2026-08-24 — A3 closed (b4b8d46), Wave 2 gate OPEN → LLM-2 ∥ UX-1 dispatched in
  parallel (disjoint surfaces; contract: only LLM-2 touches pyproject.toml/uv.lock this
  wave — pydantic v2 per decision 18). Deviation accepted: stamp-baseline-then-upgrade
  replaces plan's 'stamp head' (stamping HEAD would skip index creation on existing DBs).
  Incident note for lead spawns: two tester spawns died pre-execution on provider 429s
  (`reviewer` agent type); retry or fall back to general task agent — coverage verified
  complete by third spawn.

- 2026-08-24 — Wave 2 closed: A3 (b4b8d46) → LLM-2 (eae7bb4) ∥ UX-1 (0178f59), then
  BaselineFix refreshed tests/a1_route_map_baseline.json additively for LLM-2's three
  admin capability routes (143150e; delta verified exact, full suite 46p/1s, ruff clean).
  Deviations accepted & recorded in commit messages/lead reports: probe treats
  schema-invalid args inside valid tool_calls envelope as supports_tools=False (Res 11);
  auto-probe trigger deferred to AgenticCore cohort creation (decision 2 placement);
  supports_streaming NULL until LLM-4 transport exists; UX component rules stay in
  style.css (tokens.css pure primitives+roles); htmx vendored at first consumer (UX-2);
  ~30-line token-styled bootstrap compat shim until UX-2/UX-4. Cross-lead parallel run
  clean: no file collisions; C9 route-equality asterisk resolved by baseline refresh.

- 2026-08-24 — A4 closed (b1ab61b1), Wave 3 gate OPEN. Three lanes dispatched in
  parallel: LeadAC01 ∥ LeadUX23 ∥ LeadLLM3. Coordination contract issued to AC01/LLM-3:
  models.py appends as marked sections only, hub handshake before edits, single alembic
  head before either commits (merge revision if branched); testers verify single head +
  fresh/stamped upgrade paths on throwaway DBs only. UX lane barred from
  models/migrations/llm/agents surfaces; Res 4 rename NOT unilateral (hub to me first).

- 2026-08-25 — Wave 3 closed: A4 gate (b1ab61b1) → AC01 (c87c193) ∥ UX-2/UX-3
  (74be688, 120549a) ∥ LLM-3 (5683be1). Parallel-lane coordination contract WORKED:
  linear migration chain 359878740bb0→5b2dab0b6816→51667ad06eae→a3f1c9d2b4e7→c8f2a4e61b9d,
  single head throughout, no merge revision needed; models.py section-append handshake
  clean. Merged-tree full-gate verification commissioned (VerifyW3) since lanes' last
  full runs overlapped sibling WIP. Incidents/rulings: (1) LIVE verification artifacts
  in PROD DB — persona qa_ac1_agent + comment id=36102 (stamped agent:qa_ac1_agent)
  created by the mandated live run-once verdict; owner-visible; cleanup candidate for
  D4 moderation or manual delete. (2) reviewer-type tester agents died on provider 429s
  AGAIN (TesterUX3 ×2) — standing fallback to general task agent confirmed as policy;
  partial evidence consumed via transcript, not redone. (3) Golden-render methodology
  for UX-4+: regenerate baselines from HEAD at test start (stale /tmp goldens caused a
  false regression scare); pixel-diff HEAD-vs-working-tree is the preferred non-post
  baseline. (4) Prod DB snapshot discipline extended: pre-ac1-20260825T002043 taken +
  md5-cross-verified before AC01's prod upgrade.

- 2026-08-25 — A5 closed (a3f3a96), Wave 4 gate OPEN → LeadACP2 ∥ LeadD1 ∥ LeadUX4
  in parallel. Coordination contract (Wave-3 pattern reused): D1 and UX-4 may both add
  schema — models.py marked-section appends + hub handshake + single alembic head
  (merge revision if branched, head currently b7e4c9a02f15); ACP2 joins the handshake if
  it needs schema. Template ownership: UX4 owns public templates incl. base.html nav;
  ACP2 owns admin/* agent pages (UX-5 modernizes later) — base.html edits require hub
  handshake with UX4. Three latent deployment bugs died with A5 (see A5 row). Prod-DB
  protocol now standing: md5-ledgered snapshot before ANY prod-touching migration leg.

- 2026-08-25 — D2 closed (3deb078). Res 4 upvote_count→score rename: LeadD2c DECLINED
  the slot; orchestrator APPROVED declination WITH RE-SLOT — the rename becomes MANDATORY
  in the Wave 6 cutover commit family (AgenticCore P4 lead's brief carries it as a named
  deliverable once loader/jobs/api-ingest are deleted). Grounds recorded: §3 formulas
  fully implementable over existing score/vote_count columns; atomic rename would churn
  Resolution-11-frozen parsers, UX-5-owned admin templates/content.js, and the Wave-6-doomed
  /api/ingest wrapper; effective-score legacy semantics are keyed on upvote_count.
  D2 fix-loop lesson for testers: shipped tests must NOT reuse the module's own mirror
  as oracle (shared-blindness hid an integer-division bug until an independent mirror ran);
  independent hardcoded oracles now pinned by test_hot_sql_real_division_regression.
  SQLite note repo-wide: INT/INT '/' truncates — float literals required in SQL ranking
  fragments. Prod upgrade of d2c4f8a16e90 deferred until AC-P2 burn-in completes
  (~11:07 UTC Aug 26); app fully functional without it, feeds just run unindexed until then.
