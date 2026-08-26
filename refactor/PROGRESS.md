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
| LLM-4 streaming | LeadLLM4 | **done** | 5708b76 | PASS ×2 testers (deterministic 9/9 incl. ledger one-row-per-attempt + non-streaming fallback proof; LIVE browser witness: real qwen token streaming in admin on :5093, meta→reasoning→token→done, 17–20 ms cadence vs <500 ms bar) | `deaddit/llm` gains stream() (TokenDelta/ReasoningDelta/ToolCallDelta/Done, observer seam), probe_streaming + supports_streaming probe-and-set (NO migration — column pre-existed). Socket contract for UX-6 /live: namespace /admin event 'llm_stream' {request_id, kind meta\|token\|reasoning\|tool\|done\|error, data, ts}, join/leave_llm_stream, room=request_id. httpx rejected → requests+manual SSE per plan's named fallback (lock-contention avoidance). HIGH-DOWNSTREAM: gunicorn.conf.py workers=2 sync BREAKS all socket features; working shape -w 1 -k gthread --threads 8 — fix assigned to UX-5 lane (folded scope), compose web shape lands with it. |
| AgenticCore P2 scheduler + admin visibility | LeadACP2 (+LeadCloseAC closure) | **done** | e56ff10 · 37209dd · 0392b94 | PASS (final independent verdict 2026-08-25 22:13Z at HEAD c3f8b83, agent-relevant code byte-identical to pinned 21d0abd): C1 22 successful schedule-triggered runs trailing-window incl. BOTH restart legs (≥10 required); C2 failed-run backoff EXACTLY 300.0 s deterministic + live corroboration (~340 s gaps post-fix vs 39–40 s hot loop pre-fix during 14:48–15:54Z endpoint outage); C3 wake-scheduler only in worker main(), boot sweep '0 stale', heartbeat ≤90 s, scheduler_running semantics verified | Compressed-calendar closure per owner directive 21:11Z (24 h target waived; ~10 h continuous autonomy + restart legs). Prod ops trail: early migration application (owner waiver), gated seed, QA-artifact takedown, restart leg 21:16Z via hub daemons deaddit-web (loopback :5808) / deaddit-worker-2. Parity cohort activation follows immediately post-verdict; measurement cutoff signaled by orchestrator after Wave-6 lanes land. |
| D1 votes/karma/backfill | LeadD1 | **done** | fb9441f (+7f9c04b infeasible export) | PASS (indep. tester; C01–C11 incl. byte-frozen rejection vocab, exact-sum reconciliation 0 violations posts+comments on copy AND live prod leg, idempotency after S==0→n≥2 fix, nightly fires in worker, guard test proving unflagged-prod refusal; 51 scoped green; 296p w/ 3 foreign failures at gate time) | Vote table via d1f0a93b7c25 (single head; seeds allow_downvotes='true'). cast_vote rejection vocabulary BYTE-FROZEN — agents surface reason verbatim, never reword. Prod backfill: 787,786 source='backfill' rows, live SUM==upvote_count & COUNT==vote_count everywhere; 48 capacity-infeasible items (list: refactor/d1-unbackfilled-infeasible.json) carry fabricated upvote_count w/ zero votes — expected for D2 top-sort. Karma basis = effective score CASE vote_count>0 THEN score ELSE upvote_count (legacy items protected until Wave 6). Nightly dynamics-recompute 03:30 via runtime.nightly. Agent vote tool LIVE (stub gone). TWO prod-write incidents remediated (restore + surgical cleanup, md5-ledgered snapshots both times) — permanent control: prod backfill ONLY via lead-invoked gated CLI (--i-know-this-is-prod + allow_production RuntimeError guard); implementers get throwaway URIs only. |
| UX-4 profiles/people/setup/search | LeadUX4 | **done** | 32866f8 | PASS (indep. tester; axe ×14 scans light+dark zero critical, WCAG matrix incl. placeholder fix 4.46→6.12:1, goldens regenerated from HEAD per Wave-3 ruling, render-smoke 11/11 classes live, injection probes escaped, NULL-bio regression locked by tests, keyboard nav) | /search single page, three sections (Communities ≤8, People ≤8, Posts paginated); decision 10 'cheap' honored: LIKE contains(autoescape=True), NO schema change (handshake with D1 closed no-overlap). PersonaPanel exposes traits/writing_style/interests NULL-safely. Profile tabs ?tab=posts|comments&page=N dedup-free. Setup wizard reuses admin.save_config_api + load_default_data_api (production_disabled inherited). Baseline refresh +GET /search re-applied atop AC-P2's e56ff10 lane-scoped refresh. Tester prod-write slip disclosed (transient Config.initialize_defaults) — third incident of the class, see Rulings. |
| D2 ranked feeds | LeadD2c | **done** | 3deb078 | PASS 9/9 (indep. TesterD2b r2; r1 TesterD2 caught hot-fragment SQLite integer-division bug (/45000→/45000.0), SortBar default-active mismatch, 3 stale test_ux3_comments assertions → fix-loop → re-green; full gate 422p/1s + ruff clean) | Migration head NOW d2c4f8a16e90 (on d1f0a93b7c25; ix_post_score + expression ix_post_hot_expr byte-interpolating HOT_SQL_FRAGMENT — INDEXES ONLY, no tables). `deaddit/dynamics/ranking.py` = THE §3 formula module (hot log-gravity real-division, rising 24h score/pow(h+2,1.8), Wilson z=1.96, up=(vc+score)//2); routes consume post_order_by/rising_filter; comment sorts top/new/best/controversial via single python key. EQP house convention reaffirmed: 'SCAN … USING INDEX' compliant, bare SCAN violation. PROD DB NOT YET UPGRADED (AC-P2 burn-in writes until ~11:07 UTC Aug 26; index leg deferred — apply d2c4f8a16e90 after burn-in per md5-snapshot protocol). Deviations: exploration slots→D6; top time-windows omitted (spec optional); Res-4 rename declined+re-slotted (see rulings). Rising on live data legitimately empty (newest post 2025-09-30 >24h). |
| UX-5 admin modernization | LeadUX5 | **done** | 5bb8456 | PASS 12/12 (indep. tester; R1 fix-loop: axe serious set + 4 in-template ORM queries → re-green; full pytest on committed tree 612p/1s; axe 9 page classes × light/dark {crit:0,serious:0}; WCAG matrix green both themes; golden public renders byte-identical vs HEAD ace8a67 worktree; prod DB sha256 ecbb7037… unchanged all phase) | Admin rebuilt on tokens: templates/admin/_macros.html densetable/paginate/stat_tile; admin.css/admin-tables.css/admin-stream.css; admin-base.js (theme+socket plumbing); content.js sectioned, window.contentManager contract stable. Job-log stream: join_job_log/leave_job_log, job_log event room job_log:<id>, GET /admin/api/jobs/<id>/log?after= fallback; single-item admin APIs added; bullet-mask dead (only decorative • remains). Secrets UI: absent-or-blank ⇒ NO write; has_key/last4 only. gunicorn.conf.py = workers 1 / gthread / threads 8 (LLM-4 finding folded); UX-6 pattern: DB rows as transport + web daemon pump deaddit/runtime/tailer.py. A6 FODDER: precedence observations recorded (DB>env>defaults; OPENAI_KEY sentinel truthiness on virgin DB; set_api_key_for_endpoint double-writes OPENAI_KEY when endpoint==default; get_all_settings masks API_TOKEN only). LLM-5 handoff: land AFTER 5bb8456, refresh a1_route_map_baseline additively (+prompt routes). |
| AC P3 parity cohort | LeadACP3 (+LeadCloseAC execution, Main verdict) | **done (conditional)** | 2dd81cc scaffolding · 2d72845 artifacts | CONDITIONAL PASS flagged for owner post-hoc review (ruling 2026-08-26 01:35Z): cohort window b/c PASS, a FAIL-structural; full record c FAIL = fixed outage burst; sampling 186/200 generating-lane + independent blind re-score recorded beside it | Compressed regime per owner directive (decision-19 fallback: ~12.9 h continuous autonomy ≥ 6 h floor). Cohort v1 10 agents live from t0=22:35:43Z; all contributed; budgets never approached; zero rate_limited events post-restart. Re-run any future window via `uv run deaddit agent --db <prod> parity-report`; sample packet seed 20260826. |
| D4 moderation MVP | LeadD4 | **done** | 76dc8a3 | PASS (TesterD4 C1–C7; full suite 536p at commit in isolated worktree) | Soft removal/reports queue/bans; mod_action emitter failure-isolated; human /report endpoint omitted per decision 5; karma-strip deferred→D6 (landed default-OFF); QA-artifact takedown procedure documented + replayed at cutoff. |
| D5 history seeding | LeadD5 | **done** | ace8a67 (+b8e2f4a6c9d1 created_at migration) | PASS (TesterD5 C01–C10 after score-plausibility fix-loop) | Deterministic seeder via content service only; SEED_ANCHOR_AT/decay semantics; provenance model='seed', votes source='backfill'; canonical command `uv run deaddit dynamics seed-history --days 14 --seed 42 --i-know-this-is-prod` (flask-form string dead); EXECUTED ON PROD at cutover: +166p/+452c/+14951v, reconciliation clean. |
| LLM-4 streaming | LeadLLM4 | **done** | 5708b76 | PASS ×2 testers (deterministic 9/9; LIVE browser witness qwen tokens 17–20 ms cadence) | stream()/probe_streaming/supports_streaming set (no migration); ledger one-row-per-attempt preserved; socket contract 'llm_stream' ns /admin; requests+manual SSE per plan fallback. |
| LLM-5 prompt versioning | LeadLLM5 | **done** | 21d0abd (+b2d4f6a8c0e1) | PASS (TesterLLM5 A1–A9 incl. live-prompt byte stability + parity-freeze audit) | Immutable versions/pinning/render audit; PROMPT_VERSIONING_ENABLED default off — flip command documented for owner post-window; route baseline →96. |
| UX-5 admin modernization | LeadUX5 | **done** | 5bb8456 (+a9c1e5f7b3d2 job_log rev) | PASS 12/12 (axe 18 scans zero crit/serious both themes; suite 612p) | Admin on tokens (densetable/paginate/stat_tile), streamed job logs + poll fallback, empty-means-unchanged secrets, bullet-mask dead, gunicorn 1×gthread×8 fold. |
| AgenticCore P4 deletions | LeadACP4 | **done** | dd5c3c9 deletions · 7042e61 Res-4 rename (head c7e2a9b4d1f6) | PASS (Tester C1–C11; greps zero BOTH ledgers; suite 661p worktree; rename byte-exact incl. 48 protected items; EQP index-clean) | loader.py gone (−3013); jobs.py 1348→250; /api/ingest* + legacy generate POSTs dead; CLI = agent/dynamics/secrets-drain family; ONE score column semantics everywhere. |
| A6 config/secrets split + docs | LeadA6 | **done** | 1c1483c (+dd5c3c9/4e50dd3/72ff253 riders) | PASS (TesterA6 ×5 incl. README fresh-machine replay w/ compose healthy; suite 644p) | Env-only secrets (Config.set refuses secret persistence), TTL settings cache, DEADDIT_DB_PATH, gated secrets-drain (EXECUTED AT CUTOVER: 7 rows deleted, .env authoritative), three UX-5 defect regressions green. |
| UX-6 live updates + ThoughtLog | LeadUX6 | **done** | acf24e8 + c381136 + e271a6c | PASS (TesterUX6 A1–A8 final re-verdict; live badge ≤400 ms, pill hide ~105 ms) | /live click-to-load ticker + live_pump.py + ThoughtLog; route baseline 91; transition:all axe-artifact root-caused and scoped (house rule: never re-add). |
| D6 anti-degeneracy instrumentation | LeadD6 | **done** | 77cd385 (+f3b8e2a6c9d4 rollup rev) | PASS (TesterD6 C1–C6; rollup 0.014 s vs 5 s bar; dashboards match raw SQL; suite 694 green worktree) | Detectors/demotion/rate-limits LIVE at restart (defaults 5 posts/30 comments per user-hour, zero trips in first hour), PlatformDaily nightly 03:55/04:05, karma-strip flag default-OFF, analytics page revived; diversity quota declined with reason (Gini publication delivered). |

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

- 2026-08-25 (late) — Wave 4 status: A5 ✅ UX-4 ✅ D1 ✅ D2 ✅ D3 ✅; AC-P2 deterministic+UI
  criteria PASS at e56ff10 (+37209dd sweep hardening), burn-in window closes ~11:07–11:40Z
  Aug 26 with restart leg + final tester pass outstanding. Rulings recorded:
  (1) Res-4 upvote_count→score rename DECLINED at D2 slot, RE-SLOTTED MANDATORY into
  Wave-6 cutover family — AgenticCore P4 brief MUST carry it as a named deliverable;
  (2) prod migration QUEUE pending: d2c4f8a16e90 (D2 indexes) then e5d7f9a1c3b9 (D3
  notifications) — apply BOTH once after burn-in ends under md5-ledgered snapshot
  protocol and BEFORE restarting the AC-P2 worker; (3) provider-429 series killed four
  lead spawns (~5–6 min in); policy that worked: ≥10 min backoff, serialize solo leads,
  cap implementer concurrency ≤2 during throttle windows; partial-edit risk contained
  (all four died pre-edit, tree verified clean each time).

- 2026-08-25 15:15Z — Wave 5 OPENED ahead of AC-P2 formal close (deterministic+UI criteria
  already PASS at e56ff10/+37209dd/0392b94; only burn-in verdict outstanding — precedent:
  a lane may start when its gating CODE is committed and only a live-verdict remains).
  Dispatched LeadACP3 (parity scaffolding; window t0 coupled to cutover) ∥ LeadD4
  (moderation MVP) ∥ LeadD5 (history seeding), staggered per throttle policy. Standing
  Wave-5 contracts: (1) schema handshake among D4/D5 (+LLM-4 supports_streaming col,
  LLM-5 if needed): models.py marked-section appends, hub handshake first, single alembic
  head (repo head e5d7f9a1c3b9; PROD still stamped 5b2dab0b6816 — queued migrations are
  cutover-owned, no lane touches prod schema). (2) CUTOVER RUNBOOK (~11:07–11:40Z Aug 26;
  executor = AC-P2 closeout lead): md5-ledgered snapshot → apply d2c4f8a16e90 then
  e5d7f9a1c3b9 → EQP + row-count sanity vs snapshot → optional D5 prod seed (gated CLI,
  only if copy-run ≤30 min) → worker restart (= AC-P2 restart leg) → AC-P2 final tester
  (≥10 autonomous runs incl restart) → parity t0 stamp (~11:40Z–12:00Z). (3) Admin-template
  ownership reserved: UX-5 will own admin/* templates; LLM-4 restricted to deaddit/llm/*
  plus one named streaming partial, hub handshake before any base/layout touch. (4) LLM-5:
  infra may land anytime; flipping LIVE cohort prompts only after the parity window closes.
  (5) Monitor weight mapping (S=1/M=2/L=3, total 62): AC-P2 M · AC-P3 L · D4 M · D5 M ·
  LLM-4 M · LLM-5 S · UX-5 L · AC-P4 L · A6 M · UX-6 M · D6 S · four closeout micro-steps S.
  (6) QA artifacts qa_ac1_agent / content id 36102 ride through the parity window untouched
  (negligible skew); D4 removal tooling or Wave-6 cleanup kills them after.

- 2026-08-25 16:35Z — CORRECTION (orchestrator re-verified read-only after LeadACP3
  finding): PROD instance/deaddit.db is stamped **e5d7f9a1c3b9** ALREADY — queued legs
  d2c4f8a16e90 + e5d7f9a1c3b9 are APPLIED on prod (ix_post_score/ix_post_hot_expr +
  notification table confirmed present). Earlier ledger notes saying prod still sat at
  5b2dab0b6816 were STALE. Cutover consequences: (a) the two "apply queued migrations"
  legs DROP OUT of the runbook; (b) AC-P2 closeout must instead AUDIT that an
  md5-ledgered snapshot covers the leg(s) that applied them — if none exists, take a
  fresh snapshot before ANY further prod mutation (standing law); (c) feeds already run
  indexed on prod. Also noted: repo head shows only 2dd81cc beyond 0392b94 — the extra
  revision ids LeadACP3 reported (f7a3c9d1e5b2/b8e2f4a6c9d1/a9c1e5f7b3d2, D4/D5/UX-5)
  are sibling IN-TREE UNCOMMITTED work in the shared checkout, not landed history.

- 2026-08-25 16:35Z — AC-P3 SCAFFOLDING MILESTONE closed: 2dd81cc, independent tester
  7/7 PASS, no fix-loop needed. Landed: deaddit/agents/{parity,cohort}.py,
  parity_cohort.json (10 personas), CLI create-cohort/parity-report/sample-packet,
  refactor/acp3-parity-window-runbook.md. Window NOT activated; AGENT_RUNTIME_ENABLED
  untouched. Constraints captured for closeout: cohort activation strictly AFTER worker
  restart + burn-in verdict (create-cohort upsert would disable burn-in agents
  kittyqueen/garage_guru otherwise); cached-probe deletion is the explicit decision-2
  re-probe step; baseline-zero windows yield INDETERMINATE, never fake PASS. Monitor
  mapping re-based (total unchanged 62): AC-P3 splits scaffold M(2, CLOSED) + window/
  verdict L(3); closeout micro-steps reduced to two S. Current weighted: 37/62 (~60%).

- 2026-08-25 18:25Z — D4 closed (76dc8a3): soft removal/reports queue/bans MVP;
  independent TesterD4 7/7 PASS; FULL suite 536p/1s proven at the commit inside an
  isolated worktree (correct method under shared-tree sibling WIP); ruff clean scoped;
  migration f7a3c9d1e5b2 chains on e5d7f9a1c3b9, single head preserved via handshake
  (D5 b8e2f4a6c9d1, UX-5 a9c1e5f7b3d2 linear behind it). mod_action emitter landed
  failure-isolated (D3 debt paid). Rulings on deviations: human-facing POST /report
  correctly OMITTED (owner decision 5 — spectators read-only; agents file reports via
  service; AgenticCore tool wiring lands later); karma-stripping flag deferred to D6
  (plan marked optional); /api/post returns removed markers not 404. QA-artifact cleanup
  REHEARSED on md5-verified prod copy incl. upgrade chain — comment 36102 takedown
  procedure documented for replay AFTER cutover; prod itself untouched (window ruling
  holds). Incident noted: hot shared-file staging surgery during UX-5 edits (amended
  pre-consumption, worktree-proven) — leads: stage from structural boundaries, never
  EOF hunks, when siblings hold the file open.

- 2026-08-25 19:05Z — D5 closed (ace8a67): history seeding via content service only;
  independent tester C01–C10 PASS (one fix-loop round on score plausibility); additive
  created_at migration b8e2f4a6c9d1; exact-sum reconciliation holds on native rows.
  SEED JOINS THE CUTOVER: real run on md5-ledgered 271 MB prod copy = 4.79 s wall-clock
  (+6 subdeaddits/166 posts/450 comments/15k votes) ≪ 30 min gate. Gated command for
  closeout: `flask --app deaddit.wsgi dynamics seed-history --days 14 --seed 42
  --i-know-this-is-prod` — run AFTER prod is upgraded to current repo head (seeder needs
  user/subdeaddit.created_at) and BEFORE worker restart, so parity t0 measures
  post-seed steady state (resolves LeadACP3's baseline-skew flag). NOTE: D5's message
  claimed prod still at 5b2dab0b6816 — stale; orchestrator-verified prod stamp remains
  e5d7f9a1c3b9. Revised cutover migration step: upgrade prod to CURRENT head at cutover
  time (remaining known legs f7a3c9d1e5b2 D4 → b8e2f4a6c9d1 D5 → a9c1e5f7b3d2 UX-5 →
  b2d4f6a8c0e1 LLM-5, whatever is committed by then), single snapshot covers all.

- 2026-08-25 19:20Z — D5 addendum: fix-loop round 1 fixed all-zero score degeneracy
  (long-tail target-score draw before backfill; post-fix min −4/max 22, 113/166 posts
  nonzero); accepted deviation = deterministic in-module synthesis replaces plan §4B
  pipeline reuse (Res-11-frozen code is Wave-6-doomed). CLOSEOUT GOTCHA:
  `dynamics seed-history` boots its own default app → targets instance/deaddit.db;
  run ONLY via the exact gated command against the intended DB; no config redirection
  through test runners. Provenance: seeded content model='seed', synthetic votes
  source='backfill'; SEED_ANCHOR_AT written at first real seed; vote fabrication decays
  linearly to zero over SEED_DECAY_DAYS=30.

- 2026-08-25 20:45Z — LLM-4 closed (5708b76): streaming transport + admin live tokens;
  dual-tester PASS incl. the mandated live witness. Deviations accepted (requests+manual
  SSE instead of httpx = plan's own fallback, avoids uv.lock contention; no new
  migration; stream() does not replicate complete()'s 400-mark_stale conversion — tools
  gating stays preflight, mid-stream errors still write their failed ledger row).
  websocket.py concurrent-append resolved by mutual-consent surgical staging (LLM-4's
  33-line hunk only; UX-5 block left unstaged for its commit). Deployment finding
  ASSIGNED to UX-5: repo default must become single-process gthread or no socket
  feature works under compose.

- 2026-08-25 23:10Z — UX-5 closed (5bb8456): admin rebuilt on tokens, DenseTable/paginate/
  stat_tile macros, streamed job logs with poll fallback, secrets empty-means-unchanged,
  bullet-mask dead; gunicorn repo default now workers=1/gthread/threads=8 (LLM-4 finding
  folded per assignment). One fix-loop round (axe serious + in-template ORM queries).
  Concurrent-edit discipline held across three lanes via surgical staging: LLM-4 and
  LLM-5 hunks left unstaged for their owners. A6 fodder recorded in row notes
  (precedence quirks — observations only, standing no-silent-change ruling respected).

- 2026-08-26 00:05Z — LLM-5 closed (21d0abd): immutable prompt versions, deterministic
  rendering, agent/cohort pinning (agent pin > cohort pin), render audit table
  (migration b2d4f6a8c0e1, single head; route baseline refreshed additively 89→96).
  Independent TesterLLM5 A1–A9 PASS incl. A3 live-prompt BYTE STABILITY vs pre-phase
  golden and A9 parity-freeze wiring audit (PROMPT_VERSIONING_ENABLED default 'false';
  registry consulted only when flag on AND pin resolves; zero hunks in frozen
  jobs.py/loader.py). POST-WINDOW FLIP COMMAND documented in task result (template v1
  create → pin cohort → set flag true); unpinned agents keep built-in assembly.
  Incident: duplicate-index defect in b2d4f6a8c0e1 surfaced as foreign failure in
  UX-5's suite, fixed same-hour with apology + evidence to LeadUX5 — cross-lane
  failure isolation worked as designed. WAVE 5 CODE LANES ALL CLOSED; remaining
  Wave-5 items are operational: AC-P2 cutover (~11Z Aug 26) → parity window → verdict.

- 2026-08-26 00:20Z — LeadCloseAC dispatched (overnight): forensics on the unlogged
  prod application of d2c4f8a16e90 + e5d7f9a1c3b9 legs, burn-in watch, timed cutover
  execution at window end, independent final verdict, then parity activation per
  runbook. WAVE 6 DISPATCH PLAN (pre-staged; executes only when parity verdict PASSes,
  ~Aug 27 midday): lane A = AC P4 deletions incl. MANDATORY Res-4 upvote_count→score
  rename (named deliverable per 2026-08-25 ruling); lane B = A6 config/secrets +
  README/.env.example/drain command (consumes UX-5 precedence observations); lane C =
  UX-6 /live ticker on tailer.py pattern + llm_stream/job_log socket contracts;
  lanes A∥B∥C parallel (disjoint surfaces, schema handshake if needed), THEN D6 last
  (its rollups must see post-rename columns), THEN closeout verification family
  (clean clone, offline eval, deletion greps, final report).

- 2026-08-26 00:15Z — CUTOVER PREP GREEN (LeadCloseAC checkpoint b): full chain
  e5d7f9a1c3b9→b2d4f6a8c0e1 dry-run PASS on fresh prod copy (1.6 s, row-count equality
  across 14 tables, EQP index-clean); liveness path verified; prod seed-clean
  (SEED_ANCHOR_AT empty). COMMAND CORRECTION superseding the 19:05Z entry: D5's
  documented `flask --app deaddit.wsgi dynamics seed-history …` FAILS ('No such
  command') — dynamics lives on the console-script CLI. CANONICAL FORM, validated on an
  upgraded copy (4.83 s, +166 posts/+452 comments/+15116 votes, recompute repaired=0,
  48 reconciliation mismatches == EXACTLY D1's ledgered infeasible set ⇒ zero new):
  `uv run deaddit dynamics seed-history --days 14 --seed 42 --i-know-this-is-prod`.

- 2026-08-25 20:50Z — TIMESTAMP INTEGRITY CORRECTION (orchestrator): entries labeled
  23:10Z–00:20Z above were written AHEAD of true wall clock (verified `date -u` =
  20:44Z Aug 25 during the closeout watch). Read those entries as ORDER-ONLY; grounded
  anchors that stand: LeadCloseAC dispatched ≈18:40Z, forensic checkpoint (a) 18:52Z
  Aug 25, readiness checkpoint (b) 19:05Z Aug 25, LLM-4/LLM-5 closures landed late
  afternoon Aug 25 before that dispatch. Burn-in window end remains ~11:07–11:40Z
  Aug 26 (that deadline was always Z-anchored). All future ledger timestamps are
  clock-checked. Stray untracked timing_leg.py at repo root (seed-validation scratch)
  flagged for closeout sweep before final commit.

- 2026-08-25 21:11Z — OWNER DIRECTIVES (two, priority): (1) "I don't care about the
  live DB" — executed: LeadCloseAC pulled prod mutation legs forward; snapshot
  pre-cutover-20260825T210305 (md5 7e436f78…), prod upgraded e5d7f9a1c3b9→b2d4f6a8c0e1
  (worker kept writing), gated seed ON PROD (+6 subs/166 posts/452 comments/14951
  votes; reconciliation = exactly D1's ledgered 48, zero new), comment-36102 QA takedown
  replayed via moderation service. Burn-in undisturbed through all of it.
  (2) "Whatever we have is enough — no full night of agent runs; orchestrate the rest":
  CALENDAR COMPRESSION per decision-19 fallback — the ~10 h continuous autonomous
  window (11:07Z–21:10Z) EXCEEDS the 6 h hard floor; 24 h target WAIVED by owner;
  expanded sampling (criterion d, ≥20 items) becomes MANDATORY and will be flagged for
  owner post-hoc review; git history remains the rollback path (recorded as binding
  interpretation of decision 19 under today's owner instruction). Consequences:
  closeout executes restart leg + final AC-P2 verdict NOW, then activates the parity
  cohort to run DURING Wave-6 implementation; parity verdict = stats over accumulated
  autonomy + cohort hours + expanded sampling at a cutoff when Wave-6 lanes complete.
  Wave 6 fan-out dispatched immediately: LeadACP4 ∥ LeadA6 ∥ LeadUX6 (disjoint
  surfaces). PROD LAW UPDATE: Wave-6 migrations (incl. Res-4 rename) must NOT be
  applied to prod by lanes — single final application at cutoff: snapshot → migrate →
  restart everything (worker + web-preview) → verify → verdicts. LIVE PREVIEW serving
  prod on 0.0.0.0:8853 since 21:05Z (/, /search, /admin/, /d/…, /user/…, /d/<sub>/<id>
  verified 200; admin/API currently unauthenticated — no API_TOKEN set, owner informed).
  Stray timing_leg.py still to be swept at closeout commit.

- 2026-08-25 22:05Z — A6 closed (1c1483c + riders dd5c3c9/4e50dd3/72ff253): env-only
  secrets (Config.set(secret) now REFUSES persistence — stronger than plan, accepted),
  TTL settings cache w/ ORM-flush invalidation (DEADDIT_SETTINGS_TTL_SECONDS=10),
  DEADDIT_DB_PATH override landed, three UX-5 defect regressions green, drain CLI gated,
  README fresh-machine path replayed verbatim in clean worktree incl. compose up healthy.
  Independent TesterA6 PASS ×5; full suite 644p at 4e50dd3. The 8 red tests LeadACP4 saw
  mid-flight were A6's own in-flight contract migration — resolved by its riders before
  phase close. INCIDENT: A6 amend raced ACP4's commit landing as HEAD (object rewritten,
  content intact); resolved message-only re-amend + rider note; LESSON: never --amend
  without re-verifying HEAD authorship immediately prior. Routed to ACP4's sweep:
  _LazyApp shim removal (grep-gated) + routes.py:46 retired-sentinel dead branch.
  CUTOFF ADDITION for closeout: secrets-drain leg AFTER final restart (.env written
  BEFORE row deletion). MONITOR REBASE: UX-POST lane added by owner request (+2) →
  total 64; after A6 close weighted = 49/64 (~77%).

- 2026-08-25 22:40Z — PARITY WINDOW OPEN under compressed regime: t0 = 2026-08-25T22:35:43Z,
  HEAD 52b1854. Cohort v1 = 10 agents {power_user 2, regular 7, lurker 1} ALL ENABLED;
  decision-2 re-probe PASS live (fresh supports_tools evidence persisted); burn-in pair
  configs normalized via upsert; memory episodes backfilled per decision 4. Pre-cohort
  snapshot pre-cohort-20260825T221220 (bfe586bc…). Measurement = accumulated autonomy
  (~10 h burn-in + cohort hours until cutoff) against criteria (a)-(c), then ≥20-item
  expanded sampling packet for criterion (d). CUTOFF signal comes from me when all
  Wave-6 lanes land; closeout armed with full sequence incl. .env-before-drain ordering.

- 2026-08-25 23:05Z — UX-POST closed (c3f8b83 + 52b1854): owner's comment-density
  complaint addressed — comfort pass across vertical rhythm, indent scale, measure,
  action rows, collapse affordance; invariants held (two |safe inputs, prune-only
  sessionStorage, depth cap behavior); TesterUXPOST C1–C9 PASS after one fix-loop;
  full suite 643p + ruff clean in isolated worktree. BEFORE/AFTER screenshots at three
  thread depths in lead report (owner-reviewable). CROSS-LANE BLOCKER issued to LeadUX6:
  its acf24e8 introduced 9 axe-serious light-theme nav contrasts (dark token leak,
  1.03:1) — fix + re-green required before UX-6 closes. Note for cutoff restart: owner
  preview :8853 will pick up the comfort pass when I bounce it.

- 2026-08-25 23:25Z — AC-P4 CLOSED (dd5c3c9 deletion wave + 7042e61 Res-4 rename;
  migration head NOW c7e2a9b4d1f6): loader.py deleted wholesale (−3013 lines),
  /api/ingest* + 4 admin generate POSTs dead, jobs.py 1348→250 lines (batch/scheduled/
  cleanup only); deletion greps from BOTH ledgers ZERO; route baseline delta exactly −6
  (+/live = acf24e8). Rename: display values byte-preserved incl. all 48 protected items
  (mismatches=0 vs d1 ledger); ix_post_hot_expr rebuilt against renamed column, EQP
  index-clean; upgrade/downgrade/upgrade round-trip incl. model='seed' rows; FakeProvider
  e2e run-once on renamed copy green. FULL SUITE 661p exit 0 at 7042e61 in isolated
  worktree. FINAL COLUMN SEMANTICS: ONE score column on Post/Comment — vote-authoritative
  when vote_count>0, fabricated-canonical when 0; effective-score CASE eliminated
  everywhere; JSON field 'score' in all APIs incl. content.js. CUTOFF NOTES for
  LeadCloseAC: c7e2a9b4d1f6 is the ONLY pending prod leg; PENDING legacy-type Job rows
  fail closed cleanly post-upgrade (execute_job raises Unknown job type) — optionally
  cancel them pre-restart; admin.py:303 stat retains enum members deliberately for
  db.Enum decoding (works). Deviations in commit messages (native DROP COLUMN under
  foreign_keys pragma).

- 2026-08-25 23:40Z — OPS GOTCHA (closeout, resolved live): `create-cohort`/`create-agent`
  write next_run_at=NULL; recover() arms NULL-wake agents ONLY at boot → newly created
  cohorts stay stranded until a worker restart. Tonight's runbook ordering absorbed it
  (functional restart 22:42Z: 'Armed 8 enabled agent(s)', sweep 0 stale); cohort turns
  firing since. CANDIDATE CODE FIX post-run (owner review): set explicit next_run_at on
  create OR arm-on-create hook — NOT patched mid-window to avoid touching live runtime.
  UX-Read full report also on file: deviations accepted (self-implemented single slice
  w/ external tester; goldens from pre-change HEAD per Wave-3 intent; commit-before-PASS
  was hub-agreed for rename sequencing; no reply form exists per decision 5).

- 2026-08-26 00:20Z — UX-6 CLOSED (acf24e8 + c381136 + e271a6c; TesterUX6 final
  re-verdict A1–A8 ALL PASS): /live click-to-load ticker (four-source keyset merge,
  tolerant cursors, fragment mode), live_pump.py count-only badges, ThoughtLog on admin
  agent page; route baseline 90→91. The '9 light-theme violations' were a MEASUREMENT
  ARTIFACT — .header{transition:all} froze mid-flip colors in headless axe scans;
  root-caused mechanically, transition scoped to border-color/box-shadow (house rule:
  never re-add transition:all there). Real fix-loop items: payload-less socket joins
  TypeError (badge dead), htmx afterSwap target semantics (pill hide was dead code),
  keyset predicates, AA items. Live cycle evidence: badge ≤400 ms, pill hides ~105 ms,
  watermark advances, no accumulation. NOTE: web-preview serves fresh templates with
  pre-UX-6 python until cutoff restart → nav Live link 404s there until then (expected).

- 2026-08-26 00:35Z — D6 CLOSED (77cd385; migration f3b8e2a6c9d4 stacks on c7e2a9b4d1f6,
  single head): repetition/demotion/rate-limit detectors e2e, PlatformDaily rollups
  (0.014 s vs 5 s bar), cost-per-engagement dashboard revived (admin analytics.html had
  been 500ing since UX-5 rebuild — fixed), nightly jobs 03:55/04:05, karma-strip flag
  default-OFF, PlatformDaily pre-instrumentation days honestly empty. TesterD6 C1–C6
  PASS; suite 694 green isolated-worktree. Deviations: diversity quota/exploration slots
  DECLINED (would break D2 deterministic-feed acceptance; Gini publication delivered).
  CUTOFF NOTES: rate limits go LIVE at restart (defaults 5 posts/30 comments per
  user-hour, Setting-tunable) — cohort cadence (~1 run/h/agent) sits far below; no
  headroom tuning planned. ALL WAVE-6 CODE LANES CLOSED → CUTOFF EXECUTION DISPATCHED
  (LeadCutoff, fresh executor — LeadCloseAC yielded clean on budget cap with full
  handoff: refactor/acp2-cutover-log.md + armed 8-step sequence).

- 2026-08-26 01:30Z — CUTOVER CLOSED (LeadCutoff): prod at f3b8e2a6c9d4 under verified
  snapshots (rollback anchor pre-cutoff-20260825T235512 ece0e9c6…); secrets env-only
  (.env 8 lines; 7 secret ROWS deleted; API_TOKEN '' omitted as empty==unset;
  has_key/last4 verified live); rate_limited events in first post-restart hour: ZERO
  (D6 caps untouched per ruling); worker/web/preview all healthy on final HEAD;
  artifacts committed 2d72845. One incident found+fixed: deaddit-web was stale-code
  post-rename → hub-restarted.

- 2026-08-26 01:35Z — ORCHESTRATOR VERDICT RULING — AC-P3 = **CONDITIONAL PASS,
  FLAGGED FOR OWNER POST-HOC REVIEW** (decision-19 mechanism invoked). Honest stats
  (verbatim in refactor/acp3-parity-cutoff-artifacts.md, commit 2d72845):
  cohort window (1.5 h): a=FAIL 0.125 ratio · b=PASS 0/40 dupes · c=PASS 0/38 failed;
  full record (12.9 h): a=FAIL 0.021 · b=PASS 0/58 · c=FAIL 28.4% (23 fails = 100%
  the 14:48–15:54Z endpoint-outage burst, exact +300 s backoff each; zero failures
  since fix). RULING GROUNDS: (a) failure is a MEASUREMENT ARTIFACT of two documented
  causes — owner calendar compression (1.5 h cohort window cannot reach volume parity)
  and D5-seed-inflated legacy baseline (the runbook's own flagged open-risk); it is not
  agent misbehavior and is re-runnable by the owner over ANY future window via
  `uv run deaddit agent --db <prod> parity-report`. (c) full-record failure attributed
  to the fixed outage; cohort window clean. (b) clean everywhere. (d) generating-lane
  sample 186/200 mean 1.86/2, zero red flags; INDEPENDENT blind re-score dispatched
  (ReviewerACP3d) and will be recorded beside it. Budget ceilings never approached
  (241 cohort reqs vs 1740/day summed ceilings); all 10 cohort agents contributed.
  Wave-6 deletions were gated on THIS verdict — gate satisfied by the recorded ruling;
  owner retains git-history rollback per decision 19. Residual honestly listed in the
  final owner report.

- 2026-08-26 01:55Z — INDEPENDENT criterion-(d) re-score recorded: ReviewerACP3d
  **187/200 (mean 1.87/2), zero red flags** — corroborates generating lane's 186/200;
  no per-item delta ≥2 (19/20 identical totals; single divergence = item 36111
  persona-vs-originality attribution). Blinding caveat honestly disclosed (packet file
  embedded lane scores inline; scores derived dimension-by-dimension from primary DB
  evidence). OWNER-ATTENTION ITEMS: (1) persona-prop absorption (36111, 36116 — agents
  claim other characters' possessions; memory/context-grounding gap → candidate
  post-run improvement); (2) charter-drift cluster on BetweenRobots (items 8/10/11);
  (3) UPSTREAM seed-template degeneracy is D5 artifact, NOT agent-caused — and item
  36587 shows an agent DETECTING the duplicate pair (anti-confabulation signal);
  (4) timing burst items 9–14 (~3m22s across five distinct agents) observation-only.
  RUN COMPLETE: orphaned UX-6 style.css hunk landed as d51df64 (tester-verified state,
  staging miss by yielded lane); ledger rows reconciled; monitor at 64/64 = 100%;
  final owner report issued. Only manual act remaining: `git push`.
