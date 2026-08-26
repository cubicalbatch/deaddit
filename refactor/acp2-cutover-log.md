# AC-P2 Cutover & Parity Window Log (LeadCloseAC)

Append-only operational log. All timestamps UTC (`date -u`). Per
`refactor/acp3-parity-window-runbook.md` mid-window rule 1: every worker
restart logs `git rev-parse HEAD` + timestamp BEFORE the restart.

## Timeline

- 2026-08-25T18:52Z — Forensic audit complete: unlogged prod application of
  d2c4f8a16e90+e5d7f9a1c3b9 covered by md5-ledgered snapshot
  `pre-d3-20260825T145659` (md5 match, stamp d1f0a93b7c25 = pre-leg).
  Classified by Main: LEDGER LOGGING GAP, not protocol incident.
- 2026-08-25T19:05Z — Pre-cutover readiness checkpoint accepted (migration
  dry-run PASS, seed validation PASS w/ corrected invocation, liveness path
  confirmed).
- Burn-in watch 19:04Z→20:47Z: 16 completed autonomous runs in trailing 24 h,
  zero failures after the 14:48–15:54Z endpoint outage burst (23 failed runs,
  each backed off next_run_at by 300 s per 0392b94 behavior).
- 2026-08-25T21:03Z — OWNER WAIVER executed early mutation legs:
  snapshot `instance/deaddit.db.pre-cutover-20260825T210305`
  (md5 7e436f78e4b9dd2a81458c24f0ef6d8c, ledgered) → upgrade prod
  e5d7f9a1c3b9→b2d4f6a8c0e1 (zero failures) → EQP/row-count sanity PASS →
  gated seed PASS (+166 posts/+452 comments/+14951 votes, reconciliation
  zero new violations vs d1-unbackfilled-infeasible.json) → comment-36102
  takedown replay PASS (soft removal, persona row kept).

- 2026-08-25T21:12:25Z — PRE-RESTART STAMP (restart leg): HEAD=21d0abdcc9baa5ece3bc017969e2010ce5e8187c.
- 2026-08-25T21:12Z — RESTART LEG: graceful stop of stray bare worker
  (PID 1585674/1585677, started 09:56Z by AC-P2 burn-in setup), web+worker
  relaunched as supervised services (gunicorn workers=1/gthread/threads=8;
  web on 127.0.0.1:5808 — port 5000 occupied by unrelated service, standing
  ephemeral-port rule).

- 2026-08-25T22:13Z — FINAL AC-P2 VERDICT: **PASS** (independent tester
  LeadCloseAC.TesterACP2Final). C1 PASS: 22 successful schedule runs/24 h,
  restart legs both present (post-restart anchor run 47 @22:01:49Z).
  C2 PASS: +300 s failure backoff — code loop.py:114-118, deterministic
  scratch-DB execution measured exactly 300.0 s, live post-fix gaps ~340 s.
  C3 PASS: worker-only scheduling, clean boot sweep 21:16:09Z, liveness ~3 s.
- 2026-08-25T22:12:20Z — PRE-COHORT SNAPSHOT:
  instance/deaddit.db.pre-cohort-20260825T221220, md5 bfe586bca7a93518fcb62aaeb24c152a (== live at copy), ledger written.
- 2026-08-25T22:12–22:34Z — PARITY COHORT ACTIVATION: cached
  endpoint_capability row deleted (decision-2 re-probe forced); fresh live
  probe PASS vs http://100.84.49.52:8080/v1 qwen3.8-27b (supports_tools
  persisted from a new probe); `create-cohort --spec
  deaddit/agents/parity_cohort.json --enable` exit=0: Cohort v1 = 10 agents
  {power_user:2, regular:7, lurker:1}, all enabled; kittyqueen/garage_guru
  configs normalized (upsert); memory episodes backfilled (20/persona where
  history existed; burn-in pair inherited via backfill too). qa_ac1_agent
  remains disabled. Note: first activation attempt hit the executor's 300 s
  tool timeout mid-backfill and was re-run to completion (upsert semantics;
  no partial-state damage).
- **t0 = 2026-08-25T22:35:43Z · HEAD=52b185413f79c6563b7567ab5ad1ee6a392c0cc8**
  (parity measurement window opens; agent list snapshot in /tmp/t0-agent-list.txt)

- 2026-08-25T22:41:19Z — PRE-RESTART STAMP (cohort arming): HEAD=52b185413f79c6563b7567ab5ad1ee6a392c0cc8.
  Cause: create-cohort writes new agents with next_run_at=NULL; recover()
  arms NULL-wake agents only at worker boot, and the worker predates the
  cohort → 8 new agents stranded. Functional restart required (not gratuitous).
- 2026-08-25T22:42Z — Worker restarted (hub name deaddit-worker-2) to arm the
  cohort.

- 2026-08-25T22:55Z — STANDBY HANDOFF: all assigned closeout legs complete
  through t0; awaiting Main's CUTOFF signal for the final application
  sequence (snapshot → full-suite worktree gate → upgrade to Wave-6 head →
  .env export → worker restart + Main preview notification → parity harness
  criteria (a)-(c) + ≥20-item sample packet seed 20260826 → real
  secrets-drain LAST → post-drain verify). Post-t0 health at handoff:
  11 autonomous runs since t0, zero failures since 22:00Z, heartbeat fresh.
  Hub daemon names in use: deaddit-web (127.0.0.1:5808) / deaddit-worker-2.

- 2026-08-25T23:57:45Z — CUTOFF EXECUTION (LeadCutoff): snapshot
  `instance/deaddit.db.pre-cutoff-20260825T235512` (md5 ece0e9c6d920832db6c7bdecc2f93423,
  sqlite3 .backup while worker live — live-file md5 e9382d9a… differs only by WAL writes;
  ledger written) → full-suite gate exit 0 (694 collected, isolated worktree @77cd385)
  → prod upgrade b2d4f6a8c0e1→c7e2a9b4d1f6→f3b8e2a6c9d4 zero failures. Verified:
  single head, EQP `SCAN post USING INDEX ix_post_hot_expr` (rebuilt on `score`),
  activity_event/platform_daily/degeneracy_flag created, static row counts equal
  (users 99 / subdeaddits 30), 48 protected items mismatches=0 vs d1 ledger,
  166 seed-model posts intact. .env export reviewed (7 rows) and written to repo-root
  `.env` BEFORE deletion; DB rows still in place.
- 2026-08-25T23:59:22Z — PRE-RESTART STAMP (cutoff restart leg): HEAD=77cd38529657a91bf019df9104c5e03b35b9e137.
  PENDING legacy-type Job rows found: 0 (job census: 1327 CREATE_COMMENT COMPLETED,
  11 CREATE_POST, 1 CREATE_USER, 1 CANCELLED — nothing to cancel). Worker restart follows.
  Known gotcha for ops docs: create-cohort leaves next_run_at NULL on new
  agents — a worker restart is required to arm them.

- 2026-08-26T00:03:37Z — CUTOVER RESTART LEG COMPLETE (LeadCutoff): worker
  deaddit-worker-2 restarted cleanly at 23:59:34Z — boot sweep 0 stale, nightly
  jobs registered (rollup 03:55, degeneracy-scan 04:05, +recompute/notification-purge/
  ban-expiry), all 10 cohort agents already armed (agents_armed=0 = zero NULL-wake),
  post-restart run #82 completed 00:00:32Z, WORKER_HEARTBEAT_AT fresh (14 s).
  PENDING legacy-type Job rows cancelled: 0 existed.
- 2026-08-26T00:10Z — PARITY ARTIFACTS (LeadCutoff): full-record window
  11:07Z→00:03:37Z (12.9 h) + cohort-only sub-window; criteria verbatim + reviewer
  packet seed 20260826 (20 items, reviewer PASS 186/200, zero red flags) in
  refactor/acp3-parity-cutoff-artifacts.md + refactor/acp3-sample-packet-seed20260826.md.
- 2026-08-26T00:18Z — REAL SECRETS DRAIN (LeadCutoff, LAST prod mutation):
  `deaddit secrets-drain --i-know-this-is-prod` → found=7 removed=7; export was
  written to repo-root `.env` BEFORE deletion (byte-identical to dry-run). Post-drain:
  zero secret rows in Setting (masked query clean); live get-endpoint-key on :8853
  returns {has_key:true,last4:"5ceb",success:true} resolved from env; worker runs
  88–90 completed post-drain (endpoint accepts keyless requests — verified 200);
  heartbeat fresh. CUTOVER SEQUENCE CLOSED.

- 2026-08-26T00:20Z — INCIDENT + RESOLUTION (LeadCutoff): post-drain sweep found
  deaddit-web (:5808) returning 500 on / — root cause: process had been running
  pre-cutover code since 21:13Z; its loaded models still referenced
  `post.upvote_count`, removed by the Res-4 rename leg (sqlite3.OperationalError:
  no such column). NOT a data issue — the DB was correct. hub restart of
  deaddit-web onto final HEAD at 00:20Z → :5808 200, preview :8853 200,
  worker heartbeat fresh (8 s). LESSON for ops docs: every long-lived daemon must
  be restarted after the rename leg, not just worker + public preview.
